# 모니터링 IAM 역할 스택

[github-monitoring-role.yaml](github-monitoring-role.yaml)은 `GitHubActionsMonitoringRole` 역할 하나를 생성한다. 모니터링 데이터 수집 기능이나 GitHub Secrets를 생성하는 스택은 아니다. 실제 AWS 배포는 아직 수행하지 않았다.

## 스택 구성

| 항목 | 설정 |
| --- | --- |
| 역할 이름 | 기본 `GitHubActionsMonitoringRole`, 파라미터로 변경 가능 |
| 사용 주체 | GitHub OIDC, 기본값은 `dlask913/aws`의 `main` |
| 기존 OIDC 공급자 | 배포 계정의 `token.actions.githubusercontent.com` 재사용 |
| audience | `sts.amazonaws.com` |
| 임시 자격 증명 최대 세션 | 1시간 |
| 연결 정책 | 아래 AWS 관리형 정책 4개 |
| 출력 | 역할 ARN·역할 이름·적용한 subject |
| 삭제 | 스택을 삭제하면 생성한 역할도 삭제. 기존 공급자와 AWS 관리형 정책 자체는 삭제하지 않음 |

정책은 `AmazonS3ReadOnlyAccess`, `CloudWatchReadOnlyAccess`, `AWSConfigUserAccess`, `ComputeOptimizerReadOnlyAccess`다. 현재 수집기의 CloudTrail 조회는 `AWSConfigUserAccess`에 포함된 `cloudtrail:LookupEvents`를 사용한다. RDS 로그는 `CloudWatchReadOnlyAccess`의 `logs:FilterLogEvents`로 읽는다.

서비스 중심으로 범위를 줄인 절충안이다. 모든 연결 정책이 해당 서비스의 조회 API에만 엄격하게 한정되지는 않는다. S3 객체 내용·다른 로그 그룹을 읽을 수 있고, Config의 `Deliver*`처럼 보조 작업도 포함된다. 특정 버킷·리소스·리전 제한은 설정하지 않았다. AWS 관리형 정책의 내부 권한은 AWS가 업데이트할 수 있다.

## 배포 전 확인

1. IAM → 자격 증명 공급자에 `token.actions.githubusercontent.com`이 있고, audience에 `sts.amazonaws.com`이 등록되어 있어야 한다. 없다면 기존 계정 관리 절차로 먼저 생성한다.
2. 로컬 배포 프로필에는 CloudFormation 사용과 IAM 역할 생성·정책 연결 등 이 스택의 수명 주기 관리 권한이 있어야 한다. 수집용 역할을 배포용 프로필로 사용하지 않는다.
3. IAM 역할 이름은 계정 내에서 고유하다. 같은 이름으로 수동 생성한 역할이 이미 있으면 이 스택이 자동으로 인수하지 않는다. 이름을 변경하거나 별도 리소스 가져오기 절차를 사용한다. 같은 계정의 여러 리전에 이 스택을 중복 생성하지 않는다.
4. 기본 subject는 생성일과 GitHub에서 확인한 ID에 따른 `repo:dlask913@79985588/aws@1314973119:ref:refs/heads/main`이다. 사용자 지정 OIDC subject가 있다면 실제 값에 맞춘다. 이 템플릿은 브랜치 형식만 지원한다. GitHub Environment를 사용하는 구성은 파라미터 검증과 신뢰 정책을 함께 검토해야 한다.

역할 이름·신뢰 정책을 코드에서 관리하므로 이후 변경도 스택 업데이트로 적용한다. 스택 밖에서 추가한 정책은 의도한 권한 범위를 넓힐 수 있다. 기존 `ReadOnlyAccess`가 별도 역할에 붙어 있다면 그 역할을 자동으로 수정하거나 삭제하지 않는다.

## PowerShell에서 배포

`aws` 저장소 최상위에서 실행한다. `dev`는 본인이 설정한 배포용 AWS CLI 프로필 이름으로 바꾼다. 서울은 CloudFormation 스택을 관리할 리전이며, 역할에 서울만 조회할 수 있는 제한을 걸지는 않는다.

```powershell
$env:AWS_CLI_FILE_ENCODING = 'UTF-8'

aws cloudformation validate-template `
  --template-body 'file://monitoring/infra/github-monitoring-role.yaml' `
  --region ap-northeast-2 `
  --profile dev

aws cloudformation deploy `
  --template-file monitoring/infra/github-monitoring-role.yaml `
  --stack-name aws-daily-monitoring-iam `
  --capabilities CAPABILITY_NAMED_IAM `
  --region ap-northeast-2 `
  --profile dev
```

`CAPABILITY_NAMED_IAM`은 이름을 지정한 IAM 리소스를 배포한다는 확인이다. 배포자에게 부족한 IAM 권한을 추가해 주는 옵션은 아니다. 배포 전 변경 세트만 만들고 검토하려면 `deploy` 명령에 `--no-execute-changeset`을 추가한다.

역할 이름을 바꾸려면 `deploy`에 다음 옵션을 추가한다.

```powershell
--parameter-overrides MonitoringRoleName=GitHubActionsMonitoringRoleLearning
```

## 배포 결과 확인 및 GitHub 연결

```powershell
aws cloudformation describe-stacks `
  --stack-name aws-daily-monitoring-iam `
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' `
  --region ap-northeast-2 `
  --profile dev
```

최초 배포는 `CREATE_COMPLETE`, 업데이트는 `UPDATE_COMPLETE`인지 확인한다. IAM 콘솔에서 생성된 역할의 관리형 정책 4개와 신뢰 조건도 확인한다.

출력의 `MonitoringRoleArn`을 GitHub → Settings → Secrets and variables → Actions → `MONITORING_ROLE_ARN`에 등록한다. AWS 계정 ID나 액세스 키를 YAML에 직접 넣을 필요는 없다.

역할 스택을 배포해도 수집 코드가 자동으로 main에 반영되거나 예약 실행이 활성화되지는 않는다. [상위 설정 안내](../SETUP.ko.md)에 따라 main 반영과 수집 대상 설정 후 수동 실행부터 확인한다. 정적 검사나 `validate-template` 성공만으로 실제 OIDC 발급·AWS 데이터 조회 성공이 보장되지는 않는다.

## 실습 종료 시 삭제

먼저 `MONITORING_SCHEDULE_ENABLED`, `MONITORING_PUBLICATION_ENABLED` 변수를 `false`로 바꾸고 실행 중인 작업을 확인한다. 아래 명령은 역할 스택을 삭제한다.

```powershell
aws cloudformation delete-stack `
  --stack-name aws-daily-monitoring-iam `
  --region ap-northeast-2 `
  --profile dev

aws cloudformation wait stack-delete-complete `
  --stack-name aws-daily-monitoring-iam `
  --region ap-northeast-2 `
  --profile dev
```

삭제 후 GitHub의 `MONITORING_ROLE_ARN` Secret도 정리한다. Config 기록·RDS 로그 내보내기 등 별도로 활성화한 수집 기능은 이 스택 삭제로 중지되지 않는다.

## 공식 문서

- [CloudFormation IAM 역할](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-iam-role.html)
- [AWS CLI deploy](https://docs.aws.amazon.com/cli/latest/reference/cloudformation/deploy.html)
- [GitHub OIDC subject](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims)
- [AmazonS3ReadOnlyAccess](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonS3ReadOnlyAccess.html)
- [CloudWatchReadOnlyAccess](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/CloudWatchReadOnlyAccess.html)
- [AWSConfigUserAccess](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSConfigUserAccess.html)
- [ComputeOptimizerReadOnlyAccess](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/ComputeOptimizerReadOnlyAccess.html)
