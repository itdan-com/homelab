output "vpc_id" {
  description = "Substrate VPC id."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet ids (AZ order). The cost-min node uses [0]."
  value       = module.network.public_subnet_ids
}

output "availability_zones" {
  description = "AZs the public subnets occupy."
  value       = module.network.availability_zones
}
