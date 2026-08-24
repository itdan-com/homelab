output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "CIDR block of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "IDs of the public subnets, in AZ order."
  value       = aws_subnet.public[*].id
}

output "availability_zones" {
  description = "AZ names the public subnets were placed in."
  value       = local.az_names
}
