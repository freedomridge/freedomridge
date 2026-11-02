output "instance_public_ip" {
  description = "Elastic IP: create the domain A record with this address."
  value       = aws_eip.app.public_ip
}
output "instance_id" { value = aws_instance.freedomridge_app.id }
output "public_origin" { value = var.public_origin }
output "backup_s3_uri" { value = "s3://${aws_s3_bucket.backups.id}/database" }
output "media_bucket" {
  description = "Importers take --media-uri s3://BUCKET/media: the backend reads media/ only."
  value       = aws_s3_bucket.media.id
}
output "data_volume_id" { value = aws_ebs_volume.data.id }
output "cache_volume_id" { value = aws_ebs_volume.cache.id }
output "availability_zone" { value = aws_instance.freedomridge_app.availability_zone }
