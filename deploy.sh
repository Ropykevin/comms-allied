#!/bin/bash

set -e

echo "=========================================="
echo " Allied Communication Platform Deployment"
echo "=========================================="

echo ""
echo "Pulling latest Docker image..."

docker pull ${DOCKER_IMAGE}

echo ""
echo "Stopping old containers..."

docker compose down

echo ""
echo "Starting application..."

docker compose up -d

echo ""
echo "Running database migrations..."

docker compose exec app flask db upgrade

echo ""
echo "Cleaning unused Docker images..."

docker image prune -f

echo ""
echo "=========================================="
echo " Deployment completed"
echo "=========================================="

echo ""
echo "Container status:"
docker compose ps

echo ""
echo "Application:"
echo "https://comms.alliedtravelke.com"