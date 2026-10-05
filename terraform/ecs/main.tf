# survey-agent infrastructure — ECS Fargate (a conventional ALB + Fargate layout) + an internal API.
# Two services share one cluster + ALB (host-based routing):
#   survey-agent-runtime : public respondent runtime (`survey-agent serve`, survey.example.com), autoscaled, NO Bedrock
#   survey-agent-api     : internal API gateway (`survey-agent api`, FastAPI control plane —
#                          definitions + lifecycle + read-side response seams, survey-admin.example.com), Bedrock
# Storage: MongoDB Atlas on AWS (VPC-peered); Amazon DocumentDB is the all-native alternative
# (note compatibility caveats).
#
# This is a working skeleton: fill provider/backend, VPC/subnet ids, and Atlas peering. The shapes
# (ECR lifecycle, ALB+TG /health, IAM exec/task, Secrets, autoscaling, Bedrock policy) are the
# load-bearing parts; adapt them to your own account conventions.

terraform {
  required_version = ">= 1.6"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 5.0" } }
  # backend "s3" { bucket = "survey-terraform-state" key = "survey-agent.tfstate" region = "us-east-1" }
}

variable "region" { default = "us-east-1" }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "public_subnet_ids" { type = list(string) }
variable "acm_cert_arn" { type = string } # *.example.com
variable "image_tag" { default = "latest" }

provider "aws" { region = var.region }

# ---------- ECR ----------
resource "aws_ecr_repository" "survey" {
  name                 = "survey-agent"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_lifecycle_policy" "survey" {
  repository = aws_ecr_repository.survey.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep last 10",
    selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 },
    action = { type = "expire" }
  }] })
}

# ---------- ECS cluster ----------
resource "aws_ecs_cluster" "survey" {
  name = "survey-agent"
  setting { name = "containerInsights" value = "enabled" }
}

# ---------- Secrets (survey/*) ----------
data "aws_secretsmanager_secret" "mongo_uri"       { name = "survey/mongodb-uri" }
data "aws_secretsmanager_secret" "panel_secret"  { name = "survey/panel-shared-secret" }
data "aws_secretsmanager_secret" "jwt_secret"      { name = "survey/jwt-secret" }
data "aws_secretsmanager_secret" "firebolt"        { name = "survey/firebolt" }

# ---------- IAM ----------
resource "aws_iam_role" "exec" {
  name               = "survey-agent-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy_attachment" "exec_managed" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service" identifiers = ["ecs-tasks.amazonaws.com"] }
  }
}
# Bedrock ONLY on the api task role (authoring); the public runtime role gets none.
resource "aws_iam_role" "api_task" {
  name               = "survey-agent-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "api_bedrock" {
  role = aws_iam_role.api_task.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow",
    Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    Resource = ["arn:aws:bedrock:*::foundation-model/*"]
  }] })
}
resource "aws_iam_role" "runtime_task" {
  name               = "survey-agent-runtime-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# ---------- ALB ----------
resource "aws_lb" "survey" {
  name               = "survey-agent"
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids
  idle_timeout       = 60
}
resource "aws_lb_target_group" "runtime" {
  name        = "survey-agent-runtime"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check { path = "/health" interval = 30 healthy_threshold = 2 unhealthy_threshold = 3 }
}
resource "aws_lb_target_group" "api" {
  name        = "survey-agent-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check { path = "/health" interval = 30 }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.survey.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.acm_cert_arn
  default_action { type = "forward" target_group_arn = aws_lb_target_group.runtime.arn }
}
resource "aws_lb_listener_rule" "api_host" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  action { type = "forward" target_group_arn = aws_lb_target_group.api.arn }
  condition { host_header { values = ["survey-admin.example.com"] } }
}

# ---------- Autoscaling (runtime only — fielding is spiky) ----------
resource "aws_appautoscaling_target" "runtime" {
  max_capacity       = 30
  min_capacity       = 2
  resource_id        = "service/${aws_ecs_cluster.survey.name}/survey-agent-runtime"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "runtime_rcpt" {
  name               = "survey-agent-runtime-rcpt"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.runtime.resource_id
  scalable_dimension = aws_appautoscaling_target.runtime.scalable_dimension
  service_namespace  = "ecs"
  target_tracking_scaling_policy_configuration {
    predefined_metric_specification { predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label = "${aws_lb.survey.arn_suffix}/${aws_lb_target_group.runtime.arn_suffix}" }
    target_value = 600  # requests/target — react to launch spikes faster than CPU
  }
}

# NOTE: aws_ecs_task_definition + aws_ecs_service for runtime & api, the trigger sidecar (a small
# scheduled/long-running ECS service), security groups, and CloudWatch log groups follow the same
# shapes as any standard ECS Terraform module. Add them here when wiring the account.
