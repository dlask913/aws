terraform {
  required_version = ">= 1.5, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "Aurora 클러스터를 사용할 리전"
  type        = string
  default     = "ap-northeast-2"
}

variable "cluster_identifier" {
  description = "대상이 되는 Aurora 클러스터의 식별자"
  type        = string
  default     = "aurora-schedule-demo"

  validation {
    condition = (
      can(regex("^[a-z][a-z0-9-]{0,62}$", var.cluster_identifier)) &&
      !endswith(var.cluster_identifier, "-") &&
      !strcontains(var.cluster_identifier, "--")
    )
    error_message = "클러스터 식별자는 소문자로 시작하는 1~63자의 소문자, 숫자, 하이픈이며 연속 하이픈이나 끝의 하이픈은 사용할 수 없습니다."
  }
}

# 현재 인증한 AWS 계정과 파티션을 조회
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  cluster_arn = "arn:${data.aws_partition.current.partition}:rds:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster:${var.cluster_identifier}"
}

# Lambda 서비스가 사용할 실행 역할 정의
resource "aws_iam_role" "aurora_scheduler" {
  name = "AuroraSchedulerLambdaRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# 지정한 클러스터 하나의 상태 확인, 시작, 중지만 허용
resource "aws_iam_role_policy" "aurora_control" {
  name = "ControlOneAuroraCluster"
  role = aws_iam_role.aurora_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "rds:DescribeDBClusters",
        "rds:StartDBCluster",
        "rds:StopDBCluster"
      ]
      Resource = local.cluster_arn
    }]
  })
}

# Lambda의 CloudWatch Logs 기록용 AWS 관리형 정책
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.aurora_scheduler.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

output "role_arn" {
  description = "Lambda 함수에 연결할 실행 역할 ARN"
  value       = aws_iam_role.aurora_scheduler.arn
}

output "target_cluster_arn" {
  description = "대상 클러스터의 ARN"
  value       = local.cluster_arn
}
