"""AWS 조회 → 메모리 내 분석 → 허용된 요약만 Markdown 출력.

원본 응답, SQL, 설정 파일, 예외 메시지는 출력하거나 저장하지 않는다.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time as clock

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
STATUSES = {
    "ok": "조회 완료", "no_data": "데이터 없음·정상 여부 판단 불가",
    "not_configured": "수집 대상 미설정", "disabled": "기능 비활성",
    "access_denied": "권한 부족", "failed": "수집 실패",
    "partial": "일부 데이터·판단 보류", "not_found": "대상 없음",
}
SECTIONS = {
    "s3_policy": "S3 수명 주기", "s3_standard": "S3 Standard 저장량",
    "s3_objects": "S3 객체 수", "config": "Config 구성 변경",
    "trail": "CloudTrail 변경 요청", "cpu": "EC2 CPU",
    "credits": "EC2 CPU 크레딧", "memory": "EC2 메모리",
    "network": "EC2 네트워크", "optimizer": "EC2 Compute Optimizer",
    "rds": "RDS PostgreSQL 느린 실행 로그",
}
NUMBERS = {
    "rules": "전체 규칙 수", "enabled": "활성 규칙 수", "filtered": "필터 있는 활성 규칙",
    "expiration": "만료 규칙 수", "transition": "전환 규칙 수",
    "noncurrent": "이전 버전 만료 규칙 수", "abort": "미완료 업로드 정리 규칙 수",
    "min_expiry_days": "Days 만료 최솟값(일)", "max_expiry_days": "Days 만료 최댓값(일)",
    "latest": "최신 관측값", "delta": "직전 관측 대비 차이", "gap_days": "두 관측 간격(일)",
    "snapshots": "기간 내 구성 항목 수", "changes": "관측된 상태 변경 수",
    "baseline": "시작 전 비교 기준 있음(1/0)", "config_changes": "설정 변경 수",
    "tag_changes": "태그 변경 수", "relationship_changes": "연결 변경 수",
    "other_changes": "상태·보조 설정 변경 수", "success": "오류 없는 쓰기 요청 수",
    "failure": "실패한 쓰기 요청 수", "actors": "구분된 호출 주체 수",
    "mean": "평균(%)", "max": "최대(%)", "min": "최소 잔여 크레딧",
    "coverage": "5분 구간 관측률(%)", "network_in": "수신 합계(bytes)",
    "network_out": "송신 합계(bytes)", "lookback": "AWS 분석 기간(일)",
    "risk": "1순위 권고 성능 위험(0~4)", "logged": "집계한 실행 수",
    "unparsed": "해석 불가 duration 이벤트 수", "threshold": "집계 하한(ms)",
    "query_count": "구분된 로그 SQL 수",
}
LABELS = {
    "versioning": {"Enabled", "Suspended", "Off"},
    "finding": {"Overprovisioned", "Underprovisioned", "Optimized", "NotOptimized"},
}
TIMES = {"observed", "previous", "refreshed"}


class CollectionLimit(Exception):
    pass


def day_window(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError()
    day = date.fromisoformat(value)
    start = datetime.combine(day, time(), KST).astimezone(UTC)
    return start, start + timedelta(days=1)


def alias(key, kind, value):
    digest = hmac.new(key, (kind + "\0" + value).encode(), hashlib.sha256).hexdigest()[:16]
    return kind + "-" + digest


def row(section, resource, status="ok", **kwargs):
    return {"section": section, "alias": resource, "status": status,
            "numbers": {}, "labels": {}, "times": {}, "queries": [], "actors": [], **kwargs}


def error_status(error):
    code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
        return "access_denied"
    if code in {"ResourceNotFoundException", "NoSuchBucket", "ResourceNotDiscoveredException"}:
        return "not_found"
    if code in {"OptInRequiredException", "NoAvailableConfigurationRecorderException"}:
        return "disabled"
    return "partial" if isinstance(error, CollectionLimit) else "failed"


def pages(client, method, result_key, *, token="nextToken", max_pages=50, delay=0, **kwargs):
    """빈 페이지라도 토큰이 있으면 계속. 제한 도달 시 완전한 결과로 취급하지 않음."""
    seen = set()
    for _ in range(max_pages):
        if delay:
            clock.sleep(delay)
        response = getattr(client, method)(**kwargs)
        yield from response.get(result_key, [])
        next_token = response.get(token)
        if not next_token:
            return
        if next_token in seen:
            raise CollectionLimit()
        seen.add(next_token)
        kwargs[token] = next_token
    raise CollectionLimit()


def metric(client, namespace, name, dimensions, start, end, period=300):
    response = client.get_metric_statistics(
        Namespace=namespace, MetricName=name, Dimensions=dimensions,
        StartTime=start, EndTime=end, Period=period,
        Statistics=["Sum", "SampleCount", "Minimum", "Maximum", "Average"],
    )
    return sorted((x for x in response.get("Datapoints", [])
                   if start <= x["Timestamp"] < end), key=lambda x: x["Timestamp"])


def metric_summary(points):
    samples = sum(x["SampleCount"] for x in points)
    if not samples:
        return {}
    return {"mean": sum(x["Sum"] for x in points) / samples,
            "max": max(x["Maximum"] for x in points),
            "coverage": min(100, len({x["Timestamp"] for x in points}) / 288 * 100)}


def lifecycle_summary(response, versioning):
    rules = response.get("Rules", [])
    active = [r for r in rules if r.get("Status") == "Enabled"]
    def filtered(r):
        f = r.get("Filter", {})
        return bool(r.get("Prefix") or any(v not in ("", {}, None) for v in f.values()))
    numbers = {"rules": len(rules), "enabled": len(active),
               "filtered": sum(filtered(r) for r in active),
               "expiration": sum("Expiration" in r for r in active),
               "transition": sum(bool(r.get("Transitions")) for r in active),
               "noncurrent": sum("NoncurrentVersionExpiration" in r for r in active),
               "abort": sum("AbortIncompleteMultipartUpload" in r for r in active)}
    days = [r["Expiration"]["Days"] for r in active if "Days" in r.get("Expiration", {})]
    if days:
        numbers.update(min_expiry_days=min(days), max_expiry_days=max(days))
    return {"numbers": numbers, "labels": {"versioning": versioning.get("Status", "Off")}}


def config_summary(baseline, items, start, end):
    items = sorted((x for x in items if start <= x["configurationItemCaptureTime"] < end),
                   key=lambda x: x["configurationItemCaptureTime"])
    # ARN·이름·실제 설정값은 비교에만 사용하고 공개 모델로 전달하지 않는다.
    numbers = dict(snapshots=len(items), changes=0, baseline=int(baseline is not None),
                   config_changes=0, tag_changes=0, relationship_changes=0, other_changes=0)
    def state(x):
        config = x.get("configuration", "{}")
        if isinstance(config, str):
            config = json.loads(config or "{}")
        relationships = sorted((json.dumps(r, sort_keys=True) for r in x.get("relationships", [])))
        return [config, x.get("tags", {}), relationships,
                [x.get("configurationItemStatus"), x.get("supplementaryConfiguration", {})]]
    previous = state(baseline) if baseline else None
    for item in items:
        current = state(item)
        if previous is not None:
            changed = [a != b for a, b in zip(previous, current)]
            numbers["changes"] += int(any(changed))
            for key, flag in zip(("config_changes", "tag_changes", "relationship_changes", "other_changes"), changed):
                numbers[key] += int(flag)
        previous = current
    return numbers


def postgres_summary(events, key, resource, threshold):
    groups = defaultdict(list)
    seen = set()
    unparsed = 0
    for event in events:
        event_id = event.get("eventId")
        if event_id and event_id in seen:
            continue
        seen.add(event_id)
        message = event.get("message", "")
        # PostgreSQL 표준 text 로그의 완결된 단일 행 statement/execute만 처리한다.
        match = re.search(r"\bLOG:\s+duration:\s+([0-9]+(?:\.[0-9]+)?) ms\s+"
                          r"(?:statement:|execute [^\r\n:]+:)\s*(\S[^\r\n]*)", message)
        if not match or "\n" in message.strip() or "\r" in message.strip():
            unparsed += 1
            continue
        duration = float(match[1])
        if duration >= threshold:
            # SQL 식별자와 리터럴을 포함하는 원문은 HMAC 입력으로만 사용한다.
            groups[alias(key, "sql", resource + "\0" + match[2])].append(duration)
    records = [{"alias": name, "calls": len(times), "mean_ms": sum(times) / len(times),
                "total_ms": sum(times), "max_ms": max(times)} for name, times in groups.items()]
    by_mean = sorted(records, key=lambda q: (-q["mean_ms"], q["alias"]))[:5]
    by_total = sorted(records, key=lambda q: (-q["total_ms"], q["alias"]))[:5]
    selected = {r["alias"]: r for r in by_mean + by_total}
    return {"numbers": {"logged": sum(len(v) for v in groups.values()), "unparsed": unparsed,
                        "query_count": len(groups), "threshold": threshold},
            "queries": sorted(selected.values(), key=lambda q: (-q["total_ms"], q["alias"]))}


def validate_config(config):
    if set(config) != {"version", "targets"} or config["version"] != 1:
        raise ValueError()
    if not isinstance(config["targets"], list) or len(config["targets"]) > 30:
        raise ValueError()
    allowed = {
        "s3": ({"kind", "region", "id"}, set()),
        "audit": ({"kind", "region", "id", "resource_type", "trail_lookup"}, set()),
        "ec2": ({"kind", "region", "id"}, {"memory"}),
        "rds_postgresql_logs": ({"kind", "region", "id", "threshold_ms"}, set()),
    }
    for target in config["targets"]:
        required, optional = allowed[target["kind"]]
        if not required <= set(target) or set(target) - required - optional:
            raise ValueError()
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", target["region"]):
            raise ValueError()
        if not isinstance(target["id"], str) or not target["id"] or "REPLACE" in target["id"]:
            raise ValueError()
        if target["kind"] == "rds_postgresql_logs":
            n = target["threshold_ms"]
            if type(n) not in (int, float) or not math.isfinite(n) or n < 0:
                raise ValueError()
        if "memory" in target:
            if set(target["memory"]) != {"namespace", "metric", "dimensions"}:
                raise ValueError()
    return config


class Collector:
    def __init__(self, session, key, start, end):
        self.session, self.key, self.start, self.end = session, key, start, end
        self.clients = {}
        self.rows = []

    def client(self, service, region):
        if (service, region) not in self.clients:
            from botocore.config import Config
            self.clients[service, region] = self.session.client(
                service, region_name=region,
                config=Config(retries={"mode": "standard", "max_attempts": 4},
                              connect_timeout=5, read_timeout=30))
        return self.clients[service, region]

    def safe(self, section, resource, fn):
        try:
            values = fn()
            self.rows.append(row(section, resource, **values))
        except Exception as error:
            self.rows.append(row(section, resource, error_status(error)))

    def collect(self, targets):
        for target in targets:
            resource = alias(self.key, "resource", target["region"] + ":" + target["id"])
            getattr(self, target["kind"])(target, resource)
        present = {r["section"] for r in self.rows}
        for section in SECTIONS:
            if section not in present:
                self.rows.append(row(section, "unconfigured", "not_configured"))
        return self.rows

    def s3(self, target, resource):
        bucket, region = target["id"], target["region"]
        def policy():
            client = self.client("s3", region)
            try:
                response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") != "NoSuchLifecycleConfiguration":
                    raise
                response = {"Rules": []}
            return lifecycle_summary(response, client.get_bucket_versioning(Bucket=bucket))
        self.safe("s3_policy", resource, policy)
        for section, name, storage in (("s3_standard", "BucketSizeBytes", "StandardStorage"),
                                       ("s3_objects", "NumberOfObjects", "AllStorageTypes")):
            def storage_values(name=name, storage=storage):
                points = metric(self.client("cloudwatch", region), "AWS/S3", name,
                                [{"Name": "BucketName", "Value": bucket}, {"Name": "StorageType", "Value": storage}],
                                self.end - timedelta(days=4), self.end, 86400)
                if not points:
                    return {"status": "no_data"}
                latest = points[-1]
                result = {"numbers": {"latest": latest["Average"]},
                          "times": {"observed": latest["Timestamp"].isoformat()}}
                if len(points) >= 2:
                    previous = points[-2]
                    result["numbers"].update(delta=latest["Average"] - previous["Average"],
                                             gap_days=(latest["Timestamp"] - previous["Timestamp"]).total_seconds() / 86400)
                    result["times"]["previous"] = previous["Timestamp"].isoformat()
                if latest["Timestamp"] < self.end - timedelta(days=2) or len(points) < 2:
                    result["status"] = "partial"
                return result
            self.safe(section, resource, storage_values)

    def audit(self, target, resource):
        def configuration():
            client = self.client("config", target["region"])
            statuses = client.describe_configuration_recorder_status().get("ConfigurationRecordersStatus", [])
            if not statuses or not any(s.get("recording") for s in statuses):
                return {"status": "disabled"}
            args = dict(resourceType=target["resource_type"], resourceId=target["id"])
            # 보존 중인 과거 이력을 역순으로 조회하여 시작 직전 상태 하나를 찾는다.
            baseline = next(pages(client, "get_resource_config_history", "configurationItems",
                                  **args, laterTime=self.start - timedelta(microseconds=1),
                                  chronologicalOrder="Reverse", limit=1), None)
            items = list(pages(client, "get_resource_config_history", "configurationItems",
                               **args, earlierTime=self.start, laterTime=self.end,
                               chronologicalOrder="Forward", limit=100))
            numbers = config_summary(baseline, items, self.start, self.end)
            return {"numbers": numbers, "status": "ok" if baseline else "partial"}
        self.safe("config", resource, configuration)

        def trail():
            client = self.client("cloudtrail", target["region"])
            events = pages(client, "lookup_events", "Events", token="NextToken", delay=0.55,
                           LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": target["trail_lookup"]}],
                           StartTime=self.start, EndTime=self.end, MaxResults=50)
            seen, actors = set(), set()
            success = failure = 0
            for event in events:
                if event["EventId"] in seen or not self.start <= event["EventTime"] < self.end:
                    continue
                seen.add(event["EventId"])
                data = json.loads(event["CloudTrailEvent"])
                if str(data.get("readOnly", "true")).lower() != "false":
                    continue
                failure += int(bool(data.get("errorCode")))
                success += int(not data.get("errorCode"))
                identity = data.get("userIdentity", {})
                principal = identity.get("arn") or identity.get("principalId")
                if principal:
                    actors.add(alias(self.key, "actor", principal))
            return {"numbers": {"success": success, "failure": failure, "actors": len(actors)},
                    "actors": sorted(actors)[:20]}
        self.safe("trail", resource, trail)

    def ec2(self, target, resource):
        region, instance = target["region"], target["id"]
        dims = [{"Name": "InstanceId", "Value": instance}]
        def cpu(namespace="AWS/EC2", name="CPUUtilization", dimensions=None):
            points = metric(self.client("cloudwatch", region), namespace, name,
                            dimensions if dimensions is not None else dims, self.start, self.end)
            summary = metric_summary(points)
            if not summary:
                return {"status": "no_data"}
            return {"numbers": summary, "times": {"observed": points[-1]["Timestamp"].isoformat()},
                    "status": "ok" if summary["coverage"] >= 90 else "partial"}
        self.safe("cpu", resource, cpu)
        if "memory" in target:
            m = target["memory"]
            self.safe("memory", resource, lambda: cpu(m["namespace"], m["metric"], m["dimensions"]))
        else:
            self.rows.append(row("memory", resource, "not_configured"))

        def credits():
            points = metric(self.client("cloudwatch", region), "AWS/EC2", "CPUCreditBalance", dims, self.start, self.end)
            return {"numbers": {"min": min(p["Minimum"] for p in points)}} if points else {"status": "no_data"}
        self.safe("credits", resource, credits)

        def network():
            result = {}
            for name, label in (("NetworkIn", "network_in"), ("NetworkOut", "network_out")):
                points = metric(self.client("cloudwatch", region), "AWS/EC2", name, dims, self.start, self.end)
                if points:
                    result[label] = sum(p["Sum"] for p in points)
            return {"numbers": result, "status": "ok" if len(result) == 2 else "partial"}
        self.safe("network", resource, network)

        def optimizer():
            account = self.client("sts", region).get_caller_identity()["Account"]
            partition = self.session.get_partition_for_region(region)
            arn = f"arn:{partition}:ec2:{region}:{account}:instance/{instance}"
            response = self.client("compute-optimizer", region).get_ec2_instance_recommendations(instanceArns=[arn])
            if response.get("errors"):
                return {"status": "failed"}
            recommendations = response.get("instanceRecommendations", [])
            if not recommendations:
                return {"status": "no_data"}
            result = recommendations[0]
            values = {"labels": {"finding": result["finding"]},
                      "numbers": {"lookback": result["lookBackPeriodInDays"]},
                      "times": {"refreshed": result["lastRefreshTimestamp"].isoformat()}}
            options = sorted(result.get("recommendationOptions", []), key=lambda r: r.get("rank", 999))
            if options:
                values["numbers"]["risk"] = options[0]["performanceRisk"]
            if result["lastRefreshTimestamp"] < datetime.now(UTC) - timedelta(days=3):
                values["status"] = "partial"
            return values
        self.safe("optimizer", resource, optimizer)

    def rds_postgresql_logs(self, target, resource):
        def slowlogs():
            events = pages(self.client("logs", target["region"]), "filter_log_events", "events",
                           logGroupName=target["id"], startTime=int(self.start.timestamp() * 1000),
                           endTime=int(self.end.timestamp() * 1000), filterPattern='"duration:"', limit=1000)
            bounded = (e for e in events if self.start.timestamp() * 1000 <= e["timestamp"] < self.end.timestamp() * 1000)
            result = postgres_summary(bounded, self.key, resource, target["threshold_ms"])
            result["status"] = "partial" if result["numbers"]["unparsed"] else (
                "ok" if result["numbers"]["logged"] else "no_data")
            return result
        self.safe("rds", resource, slowlogs)


def validate_public(rows):
    """공개 경계: 임의 키·자유 문자열·비유한 수치는 거부한다."""
    def number(n):
        if type(n) not in (int, float) or not math.isfinite(n):
            raise ValueError()
    for r in rows:
        if set(r) != {"section", "alias", "status", "numbers", "labels", "times", "queries", "actors"}:
            raise ValueError()
        if r["section"] not in SECTIONS or r["status"] not in STATUSES:
            raise ValueError()
        if not re.fullmatch(r"resource-[0-9a-f]{16}|unconfigured", r["alias"]):
            raise ValueError()
        for k, v in r["numbers"].items():
            if k not in NUMBERS:
                raise ValueError()
            number(v)
        for k, v in r["labels"].items():
            if k not in LABELS or v not in LABELS[k]:
                raise ValueError()
        for k, v in r["times"].items():
            if k not in TIMES or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?[+-]\d{2}:\d{2}", v):
                raise ValueError()
            datetime.fromisoformat(v)
        for actor in r["actors"]:
            if not re.fullmatch(r"actor-[0-9a-f]{16}", actor):
                raise ValueError()
        for q in r["queries"]:
            if set(q) != {"alias", "calls", "mean_ms", "total_ms", "max_ms"} or not re.fullmatch(r"sql-[0-9a-f]{16}", q["alias"]):
                raise ValueError()
            for k in ("calls", "mean_ms", "total_ms", "max_ms"):
                number(q[k])


def render(rows, day, demo=False):
    validate_public(rows)
    start, end = day_window(day)
    lines = [f"# AWS 일일 요약 — {day}", "", "> 데모: 실제 AWS 조회 결과가 아닙니다." if demo else
             "> 지정한 대상만 조회했습니다. 계정 전체 감사나 실시간 장애 감지가 아닙니다.", "",
             f"- 집계: {day} 00:00~24:00 KST / {start.isoformat()} ~ {end.isoformat()} (끝 시각 제외)",
             f"- 생성: {datetime.now(UTC).isoformat()}",
             "- S3 정책과 Compute Optimizer는 조회 시점의 최신 상태입니다. 다른 지표는 위 기간을 사용합니다.", ""]
    for section, title in SECTIONS.items():
        lines += [f"## {title}", "", "| 대상 | 수집 상태 | 요약 |", "| --- | --- | --- |"]
        for r in (r for r in rows if r["section"] == section):
            parts = [f"{NUMBERS[k]}: {v:,.2f}" for k, v in r["numbers"].items()]
            parts += [f"{k}: {v}" for k, v in r["labels"].items()]
            parts += [f"{k}: {v}" for k, v in r["times"].items()]
            parts += r["actors"]
            lines.append(f"| {r['alias']} | {STATUSES[r['status']]} | {'; '.join(parts) or '—'} |")
        for r in (r for r in rows if r["section"] == section and r["queries"]):
            lines += ["", f"{r['alias']}: 평균 시간 상위 5개와 총 시간 상위 5개의 합집합(총 시간 순).", "",
                      "| SQL 별칭 | 기록된 실행 수 | 평균(ms) | 합계(ms) | 표본 최대(ms) |",
                      "| --- | ---: | ---: | ---: | ---: |"]
            for q in r["queries"]:
                lines.append(f"| {q['alias']} | {q['calls']} | {q['mean_ms']:.2f} | {q['total_ms']:.2f} | {q['max_ms']:.2f} |")
        lines.append("")
    lines += ["## 해석 시 주의", "",
              "- S3 저장량은 StandardStorage만 조회한 bytes입니다. 전체 저장 등급 합계가 아닙니다. 객체 수는 이전 버전·삭제 마커 등을 포함할 수 있습니다.",
              "- Config는 시작 직전 비교 기준과 기간 내 이력을 비교합니다. 보존 이력에 기준이 없으면 첫 상태를 변경으로 세지 않습니다. 기록 대상 제외·Daily 기록·일시 중단은 누락을 만들 수 있습니다.",
              "- CloudTrail은 ResourceName으로 검색된 관리 이벤트의 쓰기 요청입니다. 모든 API가 리소스를 색인하지 않으므로 0건이 변경 없음의 증명은 아닙니다. 호출 주체는 Config 변경 원인으로 확정하지 않았습니다.",
              "- EC2의 저사용이나 AWS 권고만으로 축소를 확정하지 않습니다. 메모리·업무 피크·디스크 지표 등을 함께 검토해야 합니다. 기본 CPU는 짧은 피크를 놓칠 수 있습니다.",
              "- RDS는 설정한 하한 이상인 단일 행 PostgreSQL 실행 로그만 집계합니다. 전체 SQL 통계가 아니며, 다중 행·다른 형식·parse/bind 이벤트는 해석 불가로 셉니다.",
              "- SQL 원문이 동일한 경우에만 같은 HMAC 별칭입니다. 리터럴이 달라지면 다른 별칭입니다. SQL 정규화·전일 대비·pg_stat_statements 수집은 이번 버전에 포함하지 않았습니다.",
              "- 원본은 파일·로그·artifact에 저장하지 않습니다. 별칭을 조사하려면 같은 키와 원본 식별자로 비공개 환경에서 HMAC을 다시 계산합니다.", ""]
    return "\n".join(lines)


def demo_rows():
    resource = "resource-0123456789abcdef"
    rows = [row(s, "unconfigured", "not_configured") for s in SECTIONS]
    rows[0] = row("s3_policy", resource, numbers={"rules": 1, "enabled": 1, "filtered": 1, "abort": 1})
    rows[3] = row("config", resource, numbers={"snapshots": 2, "changes": 1, "baseline": 1, "config_changes": 1})
    rows[4] = row("trail", resource, numbers={"success": 1, "failure": 1, "actors": 1}, actors=["actor-0123456789abcdef"])
    rows[5] = row("cpu", resource, "partial", numbers={"mean": 8.1, "max": 41.2, "coverage": 80})
    rows[-2] = row("optimizer", resource, numbers={"lookback": 14, "risk": 1}, labels={"finding": "Overprovisioned"})
    rows[-1] = row("rds", resource, numbers={"logged": 3, "query_count": 1, "threshold": 1000, "unparsed": 0},
                   queries=[{"alias": "sql-0123456789abcdef", "calls": 3, "mean_ms": 1500, "total_ms": 4500, "max_ms": 1800}])
    rows.append(row("rds", "resource-fedcba9876543210", "access_denied"))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=(datetime.now(KST).date() - timedelta(days=1)).isoformat())
    parser.add_argument("--output-dir", default="monitoring/reports")
    parser.add_argument("--config")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        start, end = day_window(args.date)
        if not args.demo and (end > datetime.now(UTC) or start < datetime.now(UTC) - timedelta(days=14)):
            raise ValueError()
        if args.demo:
            rows = demo_rows()
        else:
            import boto3
            raw = Path(args.config).read_text(encoding="utf-8") if args.config else os.environ["MONITORING_CONFIG_JSON"]
            config = validate_config(json.loads(raw))
            key = os.environ["MONITORING_ALIAS_KEY"].encode()
            if len(key) < 32 or not config["targets"]:
                raise ValueError()
            rows = Collector(boto3.Session(), key, start, end).collect(config["targets"])
        document = render(rows, args.date, args.demo)
        destination = Path(args.output_dir) / args.date[:4] / args.date[5:7] / (args.date + ".md")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(document, encoding="utf-8")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write("report_date=" + args.date + "\n")
        print("Public report generated; raw data was not written.")
        return 0
    except Exception:
        print("Report generation stopped: check configuration, dependencies and public schema.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
