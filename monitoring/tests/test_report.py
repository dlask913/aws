import copy
from datetime import timedelta
import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report


class FakeClient:
    def __init__(self, **responses):
        self.responses = {k: iter(v) for k, v in responses.items()}
        self.calls = []

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            result = next(self.responses[name])
            if isinstance(result, Exception):
                raise result
            return result
        return call


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code, "Message": "PRIVATE_NAME customer@example.com"}}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.start, self.end = report.day_window("2026-10-06")
        self.resource = "resource-0123456789abcdef"
        self.key = b"only-for-tests-not-a-live-secret!"

    def item(self, hour, value, **extra):
        return {"configurationItemCaptureTime": self.start + timedelta(hours=hour),
                "configuration": json.dumps({"privateName": value}), **extra}

    def test_kst_window(self):
        self.assertEqual(self.start.isoformat(), "2026-10-05T15:00:00+00:00")
        self.assertEqual(self.end - self.start, timedelta(days=1))

    def test_empty_page_does_not_stop_pagination(self):
        client = FakeClient(list_values=[{"items": [], "nextToken": "next"}, {"items": [1]}])
        self.assertEqual(list(report.pages(client, "list_values", "items")), [1])
        self.assertEqual(client.calls[1][1]["nextToken"], "next")

    def test_repeated_token_and_limit_fail_closed(self):
        for responses, cap in (([{"nextToken": "a"}, {"nextToken": "a"}], 50),
                               ([{"items": [1], "nextToken": "a"}], 1)):
            with self.assertRaises(report.CollectionLimit):
                list(report.pages(FakeClient(read=responses), "read", "items", max_pages=cap))

    def test_baseline_first_change_and_half_open_window(self):
        items = [self.item(24, "outside"), self.item(5, "after"), self.item(0, "during")]
        n = report.config_summary(self.item(-1, "before"), items, self.start, self.end)
        self.assertEqual(n["changes"], 2)
        self.assertEqual(n["snapshots"], 2)
        self.assertNotIn("before", json.dumps(n))

    def test_missing_baseline_is_not_a_change(self):
        n = report.config_summary(None, [self.item(1, "x")], self.start, self.end)
        self.assertEqual(n["baseline"], 0)
        self.assertEqual(n["changes"], 0)

    def test_tags_and_relations_not_ignored(self):
        n = report.config_summary(self.item(-1, "same", tags={"secret": "a"}),
                                  [self.item(2, "same", tags={"secret": "b"})], self.start, self.end)
        self.assertEqual(n["tag_changes"], 1)

    def test_lifecycle_filters_and_disabled_rules(self):
        response = {"Rules": [
            {"Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 7}},
            {"Status": "Enabled", "Filter": {"ObjectSizeGreaterThan": 0}, "Transitions": [{}]},
            {"Status": "Disabled", "Prefix": "PRIVATE", "Expiration": {"Days": 1}},
        ]}
        result = report.lifecycle_summary(response, {})
        self.assertEqual(result["numbers"]["filtered"], 1)
        self.assertEqual(result["numbers"]["min_expiry_days"], 7)
        self.assertEqual(result["labels"]["versioning"], "Off")

    def test_weighted_mean_not_mean_of_means(self):
        p = [{"Timestamp": self.start, "Sum": 100, "SampleCount": 10, "Maximum": 20},
             {"Timestamp": self.start + timedelta(minutes=5), "Sum": 100, "SampleCount": 1, "Maximum": 100}]
        n = report.metric_summary(p)
        self.assertAlmostEqual(n["mean"], 200 / 11)
        self.assertEqual(n["max"], 100)

    def test_postgres_grouping_threshold_and_deduplication(self):
        sql = "select SECRET_COLUMN from CUSTOMER_TABLE where email='private@example.com';"
        events = [{"eventId": "1", "message": "prefix LOG:  duration: 1200.0 ms  statement: " + sql},
                  {"eventId": "2", "message": "prefix LOG:  duration: 2200.0 ms  execute <unnamed>: " + sql}]
        result = report.postgres_summary(events + events[:1], self.key, self.resource, 1000)
        self.assertEqual(result["numbers"]["logged"], 2)
        self.assertEqual(result["queries"][0]["mean_ms"], 1700)
        self.assertNotIn("CUSTOMER_TABLE", json.dumps(result))
        self.assertNotIn("private@example.com", json.dumps(result))

    def test_incomplete_or_multiline_sql_is_not_claimed_complete(self):
        events = [{"message": "LOG: duration: 1200 ms parse q: SELECT private;"},
                  {"message": "LOG: duration: 1000 ms statement: SELECT\nprivate;"}]
        result = report.postgres_summary(events, self.key, self.resource, 0)
        self.assertEqual(result["numbers"]["unparsed"], 2)
        self.assertEqual(result["numbers"]["logged"], 0)

    def test_hmac_stability_and_key_separation(self):
        self.assertEqual(report.alias(self.key, "sql", "private"), report.alias(self.key, "sql", "private"))
        self.assertNotEqual(report.alias(self.key, "sql", "private"), report.alias(b"another key", "sql", "private"))

    def test_public_schema_rejects_raw_fields_strings_and_injection(self):
        base = report.row("rds", self.resource)
        variants = []
        for name, value in (("alias", "customer-name"), ("sql", "SELECT private"),
                            ("numbers", {"mean": "private@example.com"}),
                            ("numbers", {"mean": float("nan")}),
                            ("labels", {"finding": "Optimized\n| SQL |"}),
                            ("times", {"observed": "2026-01-01<script>"})):
            v = copy.deepcopy(base)
            v[name] = value
            variants.append(v)
        for variant in variants:
            with self.assertRaises(ValueError):
                report.render([variant], "2026-10-06")

    def test_access_denied_does_not_turn_into_zero_rules(self):
        collector = report.Collector(None, self.key, self.start, self.end)
        collector.clients["s3", "ap-northeast-2"] = FakeClient(
            get_bucket_lifecycle_configuration=[AwsError("AccessDenied")])
        collector.clients["cloudwatch", "ap-northeast-2"] = FakeClient(
            get_metric_statistics=[{"Datapoints": []}, {"Datapoints": []}])
        collector.s3({"id": "private-bucket", "region": "ap-northeast-2"}, self.resource)
        self.assertEqual(collector.rows[0]["status"], "access_denied")
        self.assertEqual(collector.rows[0]["numbers"], {})
        output = report.render(collector.rows, "2026-10-06")
        self.assertNotIn("PRIVATE_NAME", output)
        self.assertNotIn("private-bucket", output)

    def test_absent_lifecycle_is_zero_only_after_successful_versioning_read(self):
        collector = report.Collector(None, self.key, self.start, self.end)
        collector.clients["s3", "ap-northeast-2"] = FakeClient(
            get_bucket_lifecycle_configuration=[AwsError("NoSuchLifecycleConfiguration")], get_bucket_versioning=[{}])
        collector.clients["cloudwatch", "ap-northeast-2"] = FakeClient(
            get_metric_statistics=[{"Datapoints": []}, {"Datapoints": []}])
        collector.s3({"id": "private", "region": "ap-northeast-2"}, self.resource)
        self.assertEqual(collector.rows[0]["status"], "ok")
        self.assertEqual(collector.rows[0]["numbers"]["rules"], 0)

    def test_cloudtrail_errors_and_read_calls_are_distinct(self):
        def event(event_id, readonly, error=None):
            return {"EventId": event_id, "EventTime": self.start,
                    "CloudTrailEvent": json.dumps({"readOnly": readonly, "errorCode": error,
                                                   "userIdentity": {"arn": "private-arn"}})}
        collector = report.Collector(None, self.key, self.start, self.end)
        collector.clients["config", "ap-northeast-2"] = FakeClient(describe_configuration_recorder_status=[{}])
        collector.clients["cloudtrail", "ap-northeast-2"] = FakeClient(lookup_events=[{
            "Events": [event("a", False), event("b", "false", "Denied"), event("c", True), event("a", False)]}])
        collector.audit({"region": "ap-northeast-2", "id": "private-id", "resource_type": "AWS::EC2::Instance", "trail_lookup": "private-id"}, self.resource)
        self.assertEqual(collector.rows[0]["status"], "disabled")
        self.assertEqual(collector.rows[1]["numbers"], {"success": 1, "failure": 1, "actors": 1})
        self.assertNotIn("private-arn", report.render(collector.rows, "2026-10-06"))

    def test_limit_does_not_publish_partial_counts_as_complete(self):
        collector = report.Collector(None, self.key, self.start, self.end)
        collector.safe("trail", self.resource, lambda: (_ for _ in ()).throw(report.CollectionLimit()))
        self.assertEqual(collector.rows[0]["status"], "partial")
        self.assertEqual(collector.rows[0]["numbers"], {})

    def test_config_reads_old_baseline_and_stops_after_one(self):
        collector = report.Collector(None, self.key, self.start, self.end)
        client = FakeClient(
            describe_configuration_recorder_status=[{"ConfigurationRecordersStatus": [{"recording": True}]}],
            get_resource_config_history=[
                {"configurationItems": [self.item(-240, "before")], "nextToken": "unused"},
                {"configurationItems": [self.item(1, "after")]}])
        collector.clients["config", "ap-northeast-2"] = client
        collector.clients["cloudtrail", "ap-northeast-2"] = FakeClient(lookup_events=[{"Events": []}])
        collector.audit({"region": "ap-northeast-2", "id": "private-id", "resource_type": "AWS::EC2::Instance", "trail_lookup": "private-id"}, self.resource)
        self.assertEqual(collector.rows[0]["numbers"]["changes"], 1)
        self.assertNotIn("earlierTime", client.calls[1][1])
        self.assertEqual(client.calls[1][1]["limit"], 1)
        self.assertNotIn("nextToken", client.calls[2][1])

    def test_rds_collector_excludes_end_boundary(self):
        collector = report.Collector(None, self.key, self.start, self.end)
        message = "LOG: duration: 1500 ms statement: SELECT private;"
        collector.clients["logs", "ap-northeast-2"] = FakeClient(filter_log_events=[{"events": [
            {"eventId": "in", "timestamp": int(self.start.timestamp() * 1000), "message": message},
            {"eventId": "out", "timestamp": int(self.end.timestamp() * 1000), "message": message}]}])
        collector.rds_postgresql_logs({"region": "ap-northeast-2", "id": "private-group", "threshold_ms": 1000}, self.resource)
        self.assertEqual(collector.rows[0]["numbers"]["logged"], 1)

    def test_demo_is_labelled(self):
        output = report.render(report.demo_rows(), "2026-10-06", True)
        self.assertIn("실제 AWS 조회 결과가 아닙니다", output)

    def test_example_cannot_accidentally_call_aws(self):
        example = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text())
        with self.assertRaises(ValueError):
            report.validate_config(example)


if __name__ == "__main__":
    unittest.main()
