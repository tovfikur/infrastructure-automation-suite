# MCP Distributed Monitoring System

A production-ready distributed monitoring system that integrates with Claude AI to automatically detect, analyze, and fix infrastructure issues across multiple services.

## 🎯 Overview

The MCP (Model Context Protocol) Distributed Monitoring System provides:

- **Intelligent Error Detection**: Real-time log monitoring with pattern matching
- **AI-Powered Analysis**: Claude AI integration for error analysis and fix generation
- **Automated Remediation**: Secure fix execution with rollback capabilities
- **Multi-Service Support**: Nginx, PostgreSQL, Redis, and Odoo clients
- **Token-Efficient Processing**: Smart batching and caching to optimize Claude API usage
- **Production-Ready**: Comprehensive logging, monitoring, and failure handling

## 🏗️ Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Client MCP    │    │   Client MCP    │    │   Client MCP    │
│   (Nginx)       │    │ (PostgreSQL)    │    │   (Redis)       │
└────────┬────────┘    └────────┬────────┘    └────────┬────────┘
         │                      │                      │
         │         SSH          │         SSH          │         SSH
         └─────────┬────────────┴─────────┬────────────┴─────────┘
                   │                      │
              ┌────▼──────────────────────▼────┐
              │        Main MCP Server         │
              │  ┌─────────────────────────┐   │
              │  │    Token Manager        │   │
              │  │  (Rate Limiting &       │   │
              │  │   Batching)            │   │
              │  └─────────────────────────┘   │
              │  ┌─────────────────────────┐   │
              │  │   Queue Manager         │   │
              │  │ (Error/Fix Processing)  │   │
              │  └─────────────────────────┘   │
              │  ┌─────────────────────────┐   │
              │  │   Claude AI Client      │   │
              │  │  (Fix Generation)       │   │
              │  └─────────────────────────┘   │
              └─────────────────────────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Claude API key from Anthropic
- SSH keys for client connections

### 1. Environment Setup

```bash
# Clone the repository
git clone <your-repo-url>
cd mcp-system

# Set up environment variables
cp .env.example .env
# Edit .env with your Claude API key and other settings
```

### 2. Configuration

```bash
# Generate SSH keys for client communication
ssh-keygen -t rsa -b 4096 -f ssh-keys/mcp_key

# Update configuration files
nano configs/mcp-config.yml
# Set your Claude API key and adjust settings
```

### 3. Docker Deployment

```bash
# Start all services
docker-compose up -d

# Check service health
docker-compose ps

# View logs
docker-compose logs -f mcp-server
```

### 4. Manual Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Start the main server
export CLAUDE_API_KEY="your-api-key-here"
python main_mcp/server.py configs/mcp-config.yml

# Start individual clients
python clients/nginx_client.py configs/client-configs/nginx-client.yml
python clients/postgres_client.py configs/client-configs/postgres-client.yml
```

## 📋 Configuration

### Main Server Configuration (`configs/mcp-config.yml`)

Key configuration sections:

```yaml
# Claude API settings
claude_api_key: "${CLAUDE_API_KEY}"
claude:
  model: "claude-3-5-sonnet-20241022"
  max_retries: 3

# Token management
token_limits:
  max_tokens_per_minute: 40000
  batch_size: 5
  cache_ttl_hours: 24

# Queue processing
processing:
  error_processors: 3
  fix_executors: 2

# SSH connections
ssh_settings:
  max_connections_per_client: 5
  connection_timeout: 30
```

### Client Configuration

Each service type has its own configuration:

- **Nginx**: `configs/client-configs/nginx-client.yml`
- **PostgreSQL**: `configs/client-configs/postgres-client.yml`  
- **Redis**: `configs/client-configs/redis-client.yml`
- **Odoo**: `configs/client-configs/odoo-client.yml`

Example client config:
```yaml
client_id: "nginx-prod-01"
service_type: "nginx"
log_paths:
  - "/var/log/nginx/error.log"
health_check_interval: 60
backup_enabled: true
```

## 🔧 Service Clients

### Supported Services

| Service | Status | Features |
|---------|--------|----------|
| **Nginx** | ✅ Ready | Config validation, permission fixes, performance tuning |
| **PostgreSQL** | ✅ Ready | Connection management, vacuum operations, query optimization |
| **Redis** | ✅ Ready | Memory management, cache clearing, cluster support |
| **Odoo** | ✅ Ready | Module management, database maintenance, performance monitoring |

### Adding New Services

1. Create a new client class extending `ClientMCPServer`:

```python
from clients.base_client import ClientMCPServer

class MyServiceClient(ClientMCPServer):
    def get_log_paths(self) -> List[str]:
        return ["/var/log/myservice/error.log"]
    
    def get_service_name(self) -> str:
        return "myservice"
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        # Implement validation logic
        return True
    
    def apply_fix(self, fix_commands: List[str]) -> Dict[str, Any]:
        # Implement fix execution
        return {"success": True}
```

2. Create error patterns in `error_patterns/myservice_patterns.yml`
3. Add client configuration in `configs/client-configs/`

## 🛡️ Security

### Authentication & Authorization
- SSH key-based authentication only
- No password authentication supported
- Client-specific SSH keys recommended

### Network Security
- All communication over encrypted SSH
- No sensitive data in logs
- Configurable IP restrictions

### Fix Safety
- Pre-execution validation
- Automatic rollback on failure
- Risk-level assessment for all fixes
- Manual approval for high-risk changes

## 📊 Monitoring & Observability

### Health Endpoints
- Main server: `http://localhost:8080/health`
- Service status: `http://localhost:8080/status`
- Metrics: `http://localhost:9090/metrics`

### Logging
- Structured JSON logging
- Centralized log collection
- Per-service log rotation
- Error tracking and alerting

### Metrics
- Queue sizes and processing times
- Token usage and API rate limits
- Fix success/failure rates
- Client health status

## 🔄 Operations

### Starting Services

```bash
# Start main server
docker-compose up -d mcp-server

# Start specific clients
docker-compose up -d nginx-client postgres-client

# Scale workers
docker-compose up -d --scale error-processor=5
```

### Managing Clients

```bash
# Register a new client
curl -X POST http://localhost:8080/clients \
  -H "Content-Type: application/json" \
  -d '{"client_id": "web-01", "host": "192.168.1.10", "service_type": "nginx"}'

# Get client status  
curl http://localhost:8080/clients/web-01/status

# Remove client
curl -X DELETE http://localhost:8080/clients/web-01
```

### Queue Management

```bash
# View queue status
curl http://localhost:8080/queues/status

# Clear error queue
curl -X POST http://localhost:8080/queues/errors/clear

# Pause processing
curl -X POST http://localhost:8080/processing/pause
```

## 🔍 Troubleshooting

### Common Issues

**Claude API Rate Limits**
```bash
# Check token usage
curl http://localhost:8080/tokens/status

# Adjust rate limits in config
nano configs/token-limits.yml
```

**Client Connection Issues**
```bash
# Test SSH connection
ssh -i ssh-keys/mcp_key user@client-host

# Check client logs
docker-compose logs nginx-client
```

**High Memory Usage**
```bash
# Monitor memory usage
docker stats

# Adjust queue sizes
nano configs/mcp-config.yml
```

### Debug Mode

```bash
# Enable debug logging
export MCP_DEBUG=true
python main_mcp/server.py configs/mcp-config.yml

# Increase log verbosity
sed -i 's/level: "INFO"/level: "DEBUG"/' configs/mcp-config.yml
```

## 📚 API Reference

### REST API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | System health check |
| `GET` | `/status` | Detailed system status |
| `POST` | `/errors` | Submit error for processing |
| `GET` | `/errors/{id}` | Get error processing status |
| `POST` | `/clients` | Register new client |
| `GET` | `/clients` | List all clients |
| `DELETE` | `/clients/{id}` | Remove client |
| `GET` | `/queues/status` | Queue status and metrics |
| `GET` | `/tokens/status` | Token usage statistics |

### WebSocket Events

```javascript
// Connect to real-time events
const ws = new WebSocket('ws://localhost:8080/ws');

// Subscribe to error events
ws.send(JSON.stringify({
  type: 'subscribe',
  channel: 'errors'
}));
```

## 🧪 Testing

### Unit Tests
```bash
pytest tests/unit/
```

### Integration Tests
```bash
pytest tests/integration/
```

### Load Testing
```bash
# Generate test errors
python tools/error-generator.py --rate 100 --duration 300

# Monitor system performance
docker-compose logs -f mcp-server | grep "performance"
```

## 📈 Performance Tuning

### Scaling Guidelines

| Component | CPU | Memory | Scaling Notes |
|-----------|-----|--------|---------------|
| Main Server | 2-4 cores | 4-8GB | Vertical scaling recommended |
| Error Processors | 1 core each | 1GB each | Horizontal scaling |
| Fix Executors | 1 core each | 512MB each | Horizontal scaling |
| Clients | 0.5 core each | 256MB each | One per monitored service |

### Optimization Tips

1. **Batch Processing**: Increase batch size for similar errors
2. **Caching**: Enable response caching for repeated issues
3. **Priority Queues**: Configure priority rules for critical services
4. **Connection Pooling**: Optimize SSH connection limits

## 🤝 Contributing

### Development Setup

```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Run code formatting
black .
flake8 .

# Run type checking
mypy .
```

### Adding Features

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Add tests for your changes
4. Ensure all tests pass: `pytest`
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Documentation**: [Wiki](wiki/Home)
- **Issues**: [GitHub Issues](issues)
- **Discussions**: [GitHub Discussions](discussions)
- **Email**: support@yourcompany.com

## 🙏 Acknowledgments

- [Anthropic](https://anthropic.com) for the Claude AI API
- [Docker](https://docker.com) for containerization
- [Python](https://python.org) ecosystem contributors
- Open source monitoring tools inspiration

---

**Built with ❤️ for reliable infrastructure automation**
