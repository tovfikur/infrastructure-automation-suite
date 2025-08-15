#!/bin/bash

# Infrastructure Automation Suite Setup Script

set -e

echo "🚀 Infrastructure Automation Suite Setup"
echo "========================================"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check prerequisites
echo -e "${BLUE}Checking prerequisites...${NC}"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker is not installed. Please install Docker first.${NC}"
    exit 1
fi

# Check Docker Compose
if ! command -v docker-compose &> /dev/null; then
    echo -e "${RED}❌ Docker Compose is not installed. Please install Docker Compose first.${NC}"
    exit 1
fi

# Check Git
if ! command -v git &> /dev/null; then
    echo -e "${RED}❌ Git is not installed. Please install Git first.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ All prerequisites are installed${NC}"

# Initialize submodules
echo -e "${BLUE}Initializing Git submodules...${NC}"
git submodule update --init --recursive

# Create environment file from example
echo -e "${BLUE}Setting up environment configuration...${NC}"
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠️  Please edit .env file with your actual configuration values${NC}"
else
    echo -e "${GREEN}✅ .env file already exists${NC}"
fi

# Create necessary directories
echo -e "${BLUE}Creating necessary directories...${NC}"
mkdir -p logs/{mcp,saas,odoo}
mkdir -p backups/{mcp,odoo}
mkdir -p ssl
mkdir -p ssh-keys

# Set proper permissions
echo -e "${BLUE}Setting up permissions...${NC}"
chmod 700 ssh-keys
chmod 600 ssl 2>/dev/null || true

# Generate SSH keys for MCP system (if not exists)
if [ ! -f ssh-keys/mcp_key ]; then
    echo -e "${BLUE}Generating SSH keys for MCP system...${NC}"
    ssh-keygen -t rsa -b 4096 -f ssh-keys/mcp_key -N "" -C "mcp-system@$(hostname)"
    echo -e "${GREEN}✅ SSH keys generated${NC}"
else
    echo -e "${GREEN}✅ SSH keys already exist${NC}"
fi

# Pull latest images
echo -e "${BLUE}Pulling Docker images...${NC}"
docker-compose pull

# Build custom images
echo -e "${BLUE}Building custom Docker images...${NC}"
docker-compose build

echo -e "${GREEN}🎉 Setup completed successfully!${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo "1. Edit .env file with your configuration"
echo "2. Set your Claude API key in .env"
echo "3. Configure SSL certificates if needed"
echo "4. Run: docker-compose up -d"
echo ""
echo -e "${BLUE}Service URLs after deployment:${NC}"
echo "- MCP Dashboard: http://localhost:8080"
echo "- SaaS Manager: http://localhost:8000"
echo "- Odoo Master: http://localhost:8069"
echo "- Grafana: http://localhost:3000"
echo "- Prometheus: http://localhost:9091"
echo "- Backup Panel: http://localhost:5000"
