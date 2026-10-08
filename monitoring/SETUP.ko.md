# 실행 및 연결 방법

Python 3.12 기준이다. 현재 AWS 계정은 연결하지 않았고 실데이터 검증도 하지 않았다. 수집기는 리소스나 수집 기능을 생성·수정하지 않는다. `main` 반영과 아래 설정을 완료하기 전에는 실제 예약 수집·자동 게시가 실행되지 않는다.

## 1. AWS 연결 없이 검증

저장소 최상위에서 실행한다. 이 두 명령은 AWS SDK나 자격 증명이 필요 없다.

```powershell
python -m unittest discover -s monitoring/tests -v
python monitoring/report.py --demo --date 2026-10-06 --output-dir monitoring/tmp/demo
```

예제는 [samples/demo.md](samples/demo.md)에 있다. 데모라는 표시가 있는 합성 데이터다.

## 2. 무엇을 조회하고 어떻게 처리하는가

| 대상 | 실제 API | 처리 | 초기 버전의 경계 |
| --- | --- | --- | --- |
| S3 | `GetBucketLifecycleConfiguration`, `GetBucketVersioning` | 활성·필터·만료·전환·이전 버전·미완료 업로드 규칙 수, Days 만료 범위 | 필터 값·prefix·태그 원문 미출력. 목적에 맞는 보존 기간은 운영자가 판단 |
| S3 저장량 | CloudWatch `GetMetricStatistics` | 최근 4일 중 마지막 두 관측값과 차이·실제 관측 시각 | `StandardStorage`만의 bytes. 다른 저장 등급 합산은 미구현 |
| S3 객체 수 | 동일 API, `NumberOfObjects/AllStorageTypes` | 최신값과 직전값 차이 | 논리적인 최신 객체 수와 다를 수 있음 |
| Config | `DescribeConfigurationRecorderStatus`, `GetResourceConfigHistory` | 시작 직전 최신 기준 → 하루 이력의 설정·태그·연결·상태 비교 | 기준 없음·기록 중지 구분. 제외된 리소스 유형과 Daily 기록은 별도 확인 |
| CloudTrail | `LookupEvents` | 지정 `ResourceName` 검색, eventID 중복 제거, 읽기 제외, 성공/실패 요청과 호출 주체 HMAC 별칭 | Config와 나란히 표시하며 변경 원인 자동 확정은 하지 않음. 색인 안 된 이벤트는 누락 가능 |
| EC2 | CloudWatch `GetMetricStatistics` | 표본 수로 가중 CPU 평균, 최대, 5분 관측률, 최소 크레딧, 네트워크 합계 | EBS 포화·전일 대비·장기 추세는 미구현. 메모리는 명시한 Agent 지표만 사용 |
| EC2 권고 | `GetEC2InstanceRecommendations` | AWS 판단·분석 기간·갱신 시각·1순위 권고의 성능 위험 | 조회 시점의 최신 권고. 인스턴스 자동 변경 없음. 사양 이름·절감액은 공개하지 않음 |
| RDS | CloudWatch Logs `FilterLogEvents` | PostgreSQL duration 이벤트에서 실행 시간과 SQL의 HMAC 별칭 집계 | 단일 행 text statement/execute 로그만 지원. MySQL·전체 SQL 통계·정규화·전일 대비는 미구현 |

Config의 보존 중인 이력에 시작 전 기준이 없으면 기준 부족으로 표시한다. 무변경 증명으로 오해하지 않도록 제한을 표시한다. 수집 성공이라는 상태는 API 조회가 끝났다는 뜻이며, 기록 설정의 완전함이나 서비스 정상 상태를 보증하지 않는다.

CloudWatch Logs는 로그 이벤트 시각으로 기간을 자른다. RDS 로그 내보내기가 활성화되어 있어야 하며 늦게 도착한 로그는 재실행으로 보완한다. `threshold_ms`는 리포트 집계 하한으로, DB의 로깅 설정을 바꾸지 않는다. 실제 로깅 임계값이 더 크면 그보다 짧은 실행은 볼 수 없다.

SQL은 원문이 같을 때만 같은 별칭이다. HMAC 키가 달라지면 리소스·주체·SQL 별칭이 모두 달라진다. HMAC은 암호문 복호화 방식이 아니며 별칭만으로 원문을 복원할 수 없다. 권한 있는 운영자는 비공개 환경에서 같은 키와 원본을 넣어 `alias()`로 대조한다. 키와 매핑은 공개 저장소에 올리지 않는다.

## 3. 실제 수집 설정

인증된 AWS 계정 한 개 안에서 명시한 리전·리소스만 조회한다. 여러 계정을 순회하거나 모든 리소스를 자동 발견하지 않는다. 대상은 최대 30개, 페이지 조회는 API마다 최대 50페이지이며 제한 도달 시 완전한 집계 대신 `일부 데이터` 상태를 남긴다. 실제 조회 가능한 보고 날짜는 완료된 최근 14일이다.

1. [config.example.json](config.example.json)을 로컬 `monitoring/config.local.json`으로 복사한다.
2. 필요 없는 target을 삭제하고 실제 리전·버킷·인스턴스·Config resourceType/resourceId·로그 그룹을 입력한다. 예제의 서울 리전은 확정된 대상이 아니다.
3. AWS Console → 대상 리전 → Config에서 해당 리소스가 기록되는지, CloudWatch에서 지표·RDS 로그가 실제 존재하는지 확인한다.
4. 로컬 AWS CLI 프로필이나 기존 인증으로 먼저 실행한다. 키는 비공개 비밀번호 관리 도구에서 생성한 32자 이상의 무작위 문자열로 설정한다.

```powershell
python -m pip install -r monitoring/requirements.txt
$env:AWS_PROFILE = '본인이설정한조회프로필'
# MONITORING_ALIAS_KEY는 비공개 환경변수로 공급한다. 실제 키를 명령 기록에 붙여넣지 않는다.
python monitoring/report.py --config monitoring/config.local.json --output-dir monitoring/tmp/preview
```

실패 응답과 원본 예외는 출력하지 않는다. 상세 원인 조사는 AWS 콘솔에서 해당 API 권한과 수집 상태를 확인한다. 검증 오류가 나면 설정 파일의 키·형식·예제 자리표시자와 키 길이를 먼저 확인한다.

EC2 메모리 수집을 이미 사용한다면 `ec2` target에 다음 블록을 추가한다. namespace·metric·dimensions는 CloudWatch에 실제로 게시된 `%` 사용률 지표와 정확히 일치해야 한다. 지표가 없으면 이번 코드가 Agent를 설치해 주지는 않는다.

```json
"memory": {
  "namespace": "CWAgent",
  "metric": "mem_used_percent",
  "dimensions": [{"Name": "InstanceId", "Value": "REPLACE_INSTANCE_ID"}]
}
```

## 4. 최소 조회 역할과 OIDC

기존 배포 역할과 별도로 조회 역할을 만든다. 다음 표는 코드에서 사용하는 IAM action 목록이며 모든 권한을 `Resource: "*"`로 주는 정책 예제가 아니다.

| 수집 대상 | 필요한 IAM action | 리소스 범위 |
| --- | --- | --- |
| S3 설정 | `s3:GetLifecycleConfiguration`, `s3:GetBucketVersioning` | 선택한 버킷 ARN만 |
| CloudWatch 지표 | `cloudwatch:GetMetricStatistics` | 해당 API는 `*`; 리전 조건 활용 |
| Config | `config:DescribeConfigurationRecorderStatus`, `config:GetResourceConfigHistory` | 해당 API 권한 모델에 맞게 `*`, 조회 역할과 리전으로 제한 |
| CloudTrail | `cloudtrail:LookupEvents` | `*`; 리전 조건 활용 |
| Compute Optimizer | `compute-optimizer:GetEC2InstanceRecommendations`, `ec2:DescribeInstances` | `*`; 조회 계정·리전 제한 |
| RDS 로그 | `logs:FilterLogEvents` | 선택한 CloudWatch 로그 그룹 ARN만. 해당 API에 맞는 ARN 형식 확인 |

`sts:GetCallerIdentity`는 현재 계정을 확인하는 데 사용하며 별도 허용 정책이 필요하지 않다. S3 객체 읽기, SQL 접속, Secrets Manager 조회, Config 활성화, EC2/RDS 변경 권한은 사용하지 않는다.

OIDC 신뢰 정책은 provider `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`으로 설정하고, subject는 이 저장소의 main으로 제한한다. 이 저장소는 2026-07-28에 생성되었으며 GitHub의 새 기본 형식에 해당한다. 소유자 ID `79985588`, 저장소 ID `1314973119`를 확인했으므로 기본 subject는 **`repo:dlask913@79985588/aws@1314973119:ref:refs/heads/main`**이다. 앞선 이름만 있는 subject 예제는 정정한다. 사용자 지정 OIDC subject나 GitHub Environment를 사용하는 경우에는 실제 형식에 맞춰 신뢰 정책을 변경해야 한다. 코드의 실제 수집도 main으로 제한한다.

Compute Optimizer 권고 조회는 서비스의 권한 매핑에 따라 `ec2:DescribeInstances`도 요구하므로 함께 부여한다. SDK에서 해당 EC2 API를 직접 호출하지 않더라도 필요한 권한이다.

[GitHub immutable subject 형식](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims), [Compute Optimizer 권한 매핑](https://docs.aws.amazon.com/service-authorization/latest/reference/list_compute-optimizer.html)

[GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws), [STS GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html)

## 5. GitHub 설정과 실행

저장소 Settings → Secrets and variables → Actions에서 설정한다.

| 분류 | 이름 | 값 |
| --- | --- | --- |
| Secret | `MONITORING_ROLE_ARN` | 별도 조회 역할 ARN |
| Secret | `MONITORING_CONFIG_JSON` | 실제 대상이 들어간 설정 JSON 전체 |
| Secret | `MONITORING_ALIAS_KEY` | 고정된 32자 이상 무작위 HMAC 키 |
| Variable | `MONITORING_AWS_REGION` | OIDC 인증 단계의 기본 리전 |
| Variable | `MONITORING_SCHEDULE_ENABLED` | 처음에는 설정하지 않음. 예약 수집 승인 후 `true` |
| Variable | `MONITORING_PUBLICATION_ENABLED` | 처음에는 설정하지 않음. 자동 커밋 승인 후 `true` |

- Push 시 오프라인 테스트만 실행한다. PR 이벤트로 AWS 인증을 실행하지 않는다.
- 워크플로를 main에 반영한 뒤 Actions → AWS daily monitoring → Run workflow로 실행한다. 기본은 demo=true, publish=false다.
- 첫 실제 데이터는 로컬 preview로 검토한다. 공개 저장소의 **Actions artifact도 접근 가능한 공개 산출물**로 취급한다. 첫 실데이터 Actions 실행 전 공개할 지표에 동의했는지 확인한다.
- demo=false로 수동 실행하면 AWS 조회 후 공개 스키마를 통과한 Markdown만 artifact에 3일 보관한다. 계정 정보·원본 SQL·원본 이벤트·원본 예외는 보관하지 않는다.
- 공개 커밋은 `MONITORING_PUBLICATION_ENABLED=true`와 수동 `publish=true`를 모두 만족해야 한다. demo 결과는 커밋하지 않는다.
- 예약 수집은 `MONITORING_SCHEDULE_ENABLED=true`일 때 KST 09:17에 요청된다. 게시 변수도 true이면 보고서를 커밋한다. GitHub 예약 작업은 지연될 수 있다.
- 쓰기 권한은 별도 publish job에만 있다. `monitoring/reports/YYYY/MM/YYYY-MM-DD.md` 한 파일만 stage한다. main 보호 규칙이 직접 push를 금지하면 게시가 실패하며, 보호 규칙을 우회하지 않는다.
- main이 동시에 변경되면 push가 실패할 수 있다. 강제 push 대신 재실행한다. 같은 날짜의 보고서는 새 수집 결과로 갱신된다.

외부 Action은 확인한 전체 commit SHA로 고정했다. SDK 직접 의존성은 버전을 고정했지만 하위 의존성 전체를 hash lock한 구성은 아니다. 실제 환경의 의존성 관리 정책에 따라 lock을 추가한다.

## 6. 실데이터 검증과 비용

테스트는 API 대역 데이터로 시간 경계·페이지네이션·권한 오류·중복·집계·공개 차단을 확인한다. 실제 AWS 계정의 권한, 지표 dimensions, Config 기록 범위, PostgreSQL 로그 형식은 계정 연결 후 콘솔의 동일 기간과 대조해야 한다. SQL 형식이 맞지 않으면 `해석 불가`로 표시되며 정규식만 임의 완화하지 않는다.

Config 기록이나 RDS 로그 내보내기를 이 코드가 새로 활성화하지 않는다. 기존 수집 기능이 없다면 별도로 비용과 보존 기간을 정해야 한다. CloudWatch API·Logs 조회, Config 기록·저장 등은 AWS 과금 범위에 따라 비용이 발생할 수 있다.

[Config API](https://docs.aws.amazon.com/boto3/latest/reference/services/config/client/get_resource_config_history.html), [CloudWatch API](https://docs.aws.amazon.com/boto3/latest/reference/services/cloudwatch/client/get_metric_statistics.html), [로그 API](https://docs.aws.amazon.com/boto3/latest/reference/services/logs/client/filter_log_events.html), [Compute Optimizer API](https://docs.aws.amazon.com/boto3/latest/reference/services/compute-optimizer/client/get_ec2_instance_recommendations.html)
