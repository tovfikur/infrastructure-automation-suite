# Infrastructure Automation Suite - Deployment Guide

## 🚀 Quick Deployment

### Prerequisites
- Docker Engine 20.10+
- Docker Compose 2.0+
- 8GB RAM minimum (16GB recommended for production)
- 50GB disk space
- Claude API key from Anthropic

### 1. Clone and Setup
```bash
git clone <your-repository-url> infrastructure-automation-suite
cd infrastructure-automation-suite

# Initialize submodules
git submodule update --init --recursive

# Run setup script (Linux/macOS)
chmod +x setup.sh
./setup.sh

# Or setup manually (Windows/all platforms)
cp .env.example .env
mkdir -p logs/{mcp,saas,odoo} backups/{mcp,odoo} ssl ssh-keys
```

### 2. Configuration
```bash
# Edit environment variables
nano .env

# Required: Set your Claude API key
CLAUDE_API_KEY=your-claude-api-key-here

# Optional: Configure other services
GRAFANA_ADMIN_PASSWORD=your-secure-password
POSTGRES_SHARED_PASSWORD=your-database-password
```

### 3. Deploy
```bash
# Deploy all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                 Load Balancer (Nginx)                   │
│                     Port 80/443                        │
└┬────────────────────────────────────────────────────────┘
 │
 ├─── SaaS Manager (Port 8000)
 │    └── Tenant Management & API
 │
 ├─── Odoo Master (Port 8069)
 │    └── Main Odoo Instance
 │
 └─── Odoo Workers (Scaled)
      └── Processing Instances

┌─────────────────────────────────────────────────────────┐
│               MCP Monitoring System                      │
│                     Port 8080                          │
└┬────────────────────────────────────────────────────────┘
 │
 ├─── MCP Clients (SSH-based)
 │    ├── Nginx Monitor
 │    ├── PostgreSQL Monitor  
 │    ├── Redis Monitor
 │    └── Odoo Monitor
 │
 └─── Claude AI Integration
      └── Automated Fix Generation

┌─────────────────────────────────────────────────────────┐
│                Shared Infrastructure                     │
└┬────────────────────────────────────────────────────────┘
 │
 ├─── PostgreSQL (Port 5433)
 │    └── Shared database for all services
 │
 ├─── Redis (Port 6380)
 │    └── Shared cache and sessions
 │
 ├─── Prometheus (Port 9091)
 │    └── Metrics collection
 │
 ├─── Grafana (Port 3000)
 │    └── Monitoring dashboards
 │
 └─── Backup Panel (Port 5000)
      └── Disaster recovery management
```

## 🔧 Service Configuration

### MCP Monitoring System
```yaml
# mcp-monitoring-system/configs/mcp-config.yml
claude_api_key: "${CLAUDE_API_KEY}"
token_limits:
  max_tokens_per_minute: 40000
  batch_size: 5
processing:
  error_processors: 3
  fix_executors: 2
```

### Odoo Multi-Tenant System
```yaml
# Configured via environment variables
SAAS_SECRET_KEY: your-secret-key
ODOO_MASTER_PASSWORD: admin-password
POSTGRES_PASSWORD: database-password
```

## 🚀 Scaling Configuration

### Horizontal Scaling
```bash
# Scale Odoo workers
docker-compose up -d --scale odoo-worker1=3 --scale odoo-worker2=3

# Scale MCP processors
docker-compose up -d --scale mcp-processor=5

# Scale monitoring
docker-compose up -d --scale prometheus=2
```

### Resource Limits
```yaml
# Add to docker-compose.yml services
deploy:
  resources:
    limits:
      memory: 2G
      cpus: '1.0'
    reservations:
      memory: 1G
      cpus: '0.5'
```

## 🔐 Security Configuration

### SSL Certificate Setup
```bash
# Generate self-signed certificate (development)
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ssl/certificate.key \
  -out ssl/certificate.crt

# For production, use Let's Encrypt
certbot certonly --webroot -w /var/www/certbot \
  -d yourdomain.com
```

### SSH Key Management
```bash
# Generate MCP SSH keys
ssh-keygen -t rsa -b 4096 -f ssh-keys/mcp_key -N ""

# Copy public key to monitored servers
ssh-copy-id -i ssh-keys/mcp_key.pub user@target-server
```

## 📊 Monitoring Setup

### Grafana Dashboards
Access Grafana at http://localhost:3000
- Default credentials: admin/admin (change on first login)
- Import dashboard from monitoring/grafana/dashboards/

### Prometheus Configuration
Access Prometheus at http://localhost:9091
- Configured targets in monitoring/prometheus.yml
- Automatic service discovery enabled

## 🔄 Backup & Recovery

### Automated Backups
```bash
# Database backups (daily at 2 AM)
docker-compose exec postgres-shared pg_dump -U postgres > backup.sql

# Filestore backup
docker-compose exec backup-panel python backup.py --full

# View backup status
curl http://localhost:5000/api/backups/status
```

### Manual Recovery
```bash
# Restore database
docker-compose exec postgres-shared psql -U postgres < backup.sql

# Restore filestore
docker-compose exec backup-panel python restore.py --backup-id <id>
```

## 🛠️ Troubleshooting

### Common Issues

**Service won't start**
```bash
# Check logs
docker-compose logs <service-name>

# Check configuration
docker-compose config

# Restart service
docker-compose restart <service-name>
```

**Database connection issues**
```bash
# Check PostgreSQL status
docker-compose exec postgres-shared pg_isready -U postgres

# Reset database
docker-compose down -v
docker-compose up -d postgres-shared
```

**MCP monitoring not working**
```bash
# Check Claude API key
curl -H "Authorization: Bearer $CLAUDE_API_KEY" \
  https://api.anthropic.com/v1/models

# Check SSH connections
docker-compose exec mcp-server ssh -i /app/ssh-keys/mcp_key user@target
```

### Performance Tuning

**Database Optimization**
```sql
-- PostgreSQL tuning
ALTER SYSTEM SET shared_buffers = '512MB';
ALTER SYSTEM SET effective_cache_size = '2GB';
ALTER SYSTEM SET random_page_cost = 1.1;
SELECT pg_reload_conf();
```

**Redis Optimization**
```bash
# Increase memory limit
redis-cli CONFIG SET maxmemory 2gb
redis-cli CONFIG SET maxmemory-policy allkeys-lru
```

## 🔄 Update Procedure

### Rolling Updates
```bash
# Update infrastructure suite
git pull origin main
git submodule update --recursive

# Update specific service
docker-compose pull <service-name>
docker-compose up -d --no-deps <service-name>

# Update all services
docker-compose pull
docker-compose up -d
```

### Zero-Downtime Deployment
```bash
# Scale up new version
docker-compose up -d --scale odoo-worker=6

# Wait for health checks
sleep 60

# Scale down old version
docker-compose up -d --scale odoo-worker=3

# Update main services
docker-compose up -d --no-deps saas-manager mcp-server
```

## 📈 Production Deployment

### Environment Preparation
```bash
# Production environment file
cp .env.example .env.production

# Set production values
ENVIRONMENT=production
SSL_ENABLED=true
LETS_ENCRYPT_EMAIL=admin@yourdomain.com
POSTGRES_MAX_CONNECTIONS=500
```

### Docker Swarm Deployment
```bash
# Initialize swarm
docker swarm init

# Deploy stack
docker stack deploy -c docker-compose.yml infrastructure

# Scale services
docker service scale infrastructure_odoo-worker=5
```

### Kubernetes Deployment
```bash
# Generate Kubernetes manifests
kompose convert -f docker-compose.yml

# Deploy to cluster
kubectl apply -f ./
```

## 📞 Support & Maintenance

### Health Checks
- MCP Server: http://localhost:8080/health
- SaaS Manager: http://localhost:8000/health
- Odoo Master: http://localhost:8069/web/health
- Prometheus: http://localhost:9091/-/healthy
- Grafana: http://localhost:3000/api/health

### Log Management
```bash
# Centralized logging
docker-compose logs -f --tail=100

# Service-specific logs
docker-compose logs -f mcp-server
docker-compose logs -f saas-manager

# Log rotation
logrotate /etc/logrotate.d/docker-containers
```

### Maintenance Tasks
```bash
# Database maintenance (weekly)
docker-compose exec postgres-shared psql -c "VACUUM ANALYZE;"

# Redis cleanup (daily)
docker-compose exec redis-shared redis-cli FLUSHEXPIRED

# Docker cleanup (weekly)
docker system prune -a -f
docker volume prune -f
```
