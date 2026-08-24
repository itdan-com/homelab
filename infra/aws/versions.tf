terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40, < 7.0"
    }
  }

  # State backend. Local for authoring/validate; switch to S3 + DynamoDB
  # lock for the real deployment (uncomment and fill in after the owner's
  # bootstrap bucket exists — it is created by a separate one-time step,
  # never destroyed with the platform).
  #
  # backend "s3" {
  #   bucket         = "homelab-tfstate-<account-id>"
  #   key            = "aws/substrate.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "homelab-tflock"
  #   encrypt        = true
  # }
}
