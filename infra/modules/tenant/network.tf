# The tenant's own VPC. Public subnets only: tasks get a public IP while they run and
# a security group that allows no inbound traffic. No NAT gateway, which alone would
# cost more than everything else in this stack combined.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Strip all rules from the VPC's default security group so nothing uses it by accident.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.this.id
}

resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "Trader tasks: no inbound, outbound HTTPS and SMTP submission only"
  vpc_id      = aws_vpc.this.id
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
