# Networking. By default the tenant gets its own VPC with public subnets only: tasks get a
# public IP while they run and a security group that allows no inbound traffic. No NAT
# gateway, which alone would cost more than everything else in this stack combined.
#
# use_default_vpc = true instead runs the tasks in the account's default VPC (what the NSE
# stack uses), for accounts whose guardrails don't allow creating VPCs. Isolation then rests
# on this tenant's own security group, which allows no inbound traffic at all.

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_vpc" "default" {
  count   = var.use_default_vpc ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.use_default_vpc ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

locals {
  own_vpc    = !var.use_default_vpc
  vpc_id     = local.own_vpc ? aws_vpc.this[0].id : data.aws_vpc.default[0].id
  subnet_ids = local.own_vpc ? aws_subnet.public[*].id : slice(sort(data.aws_subnets.default[0].ids), 0, 2)
}

resource "aws_vpc" "this" {
  count                = local.own_vpc ? 1 : 0
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  count  = local.own_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count                   = local.own_vpc ? 2 : 0
  vpc_id                  = aws_vpc.this[0].id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  count  = local.own_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = local.own_vpc ? 2 : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# Strip all rules from our own VPC's default security group so nothing uses it by accident.
# (Never touch the account's default VPC this way: other stacks may rely on it.)
resource "aws_default_security_group" "default" {
  count  = local.own_vpc ? 1 : 0
  vpc_id = aws_vpc.this[0].id
}

resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "Trader tasks: no inbound, outbound HTTPS and SMTP submission only"
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.task.id
  description       = "Alpaca, AWS APIs, FX rate, ECR"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "smtp" {
  security_group_id = aws_security_group.task.id
  description       = "Report email (SMTP submission with STARTTLS)"
  ip_protocol       = "tcp"
  from_port         = 587
  to_port           = 587
  cidr_ipv4         = "0.0.0.0/0"
}
