#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Tiny Dropbox Backup Installer ===${NC}\n"

# Check if running on Linux
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    echo -e "${RED}Error: This script is designed for Linux (Ubuntu/Debian).${NC}"
    exit 1
fi

# Check if running from the project root
if [[ ! -f "pyproject.toml" ]] || [[ ! -f "README.md" ]]; then
    echo -e "${RED}Error: Please run this script from the tiny-dropbox-backup project root.${NC}"
    exit 1
fi

echo -e "${YELLOW}Step 1: Checking system dependencies...${NC}"

# Check for Python 3.11+
PYTHON_CMD=""
for py_version in python3.11 python3.12 python3.13 python3.14 python3; do
    if command -v "$py_version" &> /dev/null; then
        PYTHON_VERSION=$("$py_version" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        if [[ "$PYTHON_VERSION" == "3."* ]] && (( ${PYTHON_VERSION#*.} >= 11 )); then
            PYTHON_CMD="$py_version"
            echo -e "${GREEN}✓ Found Python $PYTHON_VERSION ($py_version)${NC}"
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    echo -e "${RED}Error: Python 3.11 or newer is required but not found.${NC}"
    echo "Install with: sudo apt install python3.11 python3.11-venv"
    exit 1
fi

# Check for venv support
if ! "$PYTHON_CMD" -m venv --help &> /dev/null; then
    echo -e "${RED}Error: Python venv module not found.${NC}"
    echo "Install with: sudo apt install ${PYTHON_CMD%-*}-venv"
    exit 1
fi

# Check for Git
if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: Git is not installed.${NC}"
    echo "Install with: sudo apt install git"
    exit 1
fi

echo -e "${GREEN}✓ System dependencies check passed${NC}\n"

echo -e "${YELLOW}Step 2: Creating Python virtual environment...${NC}"

# Create virtual environment if it doesn't exist
if [[ ! -d ".venv" ]]; then
    "$PYTHON_CMD" -m venv .venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
else
    echo -e "${GREEN}✓ Virtual environment already exists${NC}"
fi

# Activate virtual environment
source .venv/bin/activate
echo -e "${GREEN}✓ Virtual environment activated${NC}\n"

echo -e "${YELLOW}Step 3: Installing Python dependencies...${NC}"

# Upgrade pip and setuptools
python -m pip install --quiet --upgrade pip setuptools
echo -e "${GREEN}✓ pip and setuptools upgraded${NC}"

# Install the package
python -m pip install --quiet .
echo -e "${GREEN}✓ Package installed${NC}\n"

echo -e "${YELLOW}Step 4: Setting up credentials directory...${NC}"

# Create .dropbox-mirror directory in home for credentials
CRED_DIR="${HOME}/.dropbox-mirror"
if [[ ! -d "$CRED_DIR" ]]; then
    mkdir -p "$CRED_DIR"
    chmod 700 "$CRED_DIR"
    echo -e "${GREEN}✓ Credentials directory created: $CRED_DIR${NC}"
else
    echo -e "${GREEN}✓ Credentials directory already exists: $CRED_DIR${NC}"
fi

# Ensure proper permissions
chmod 700 "$CRED_DIR"

echo -e "${YELLOW}Step 5: Making wrapper scripts executable...${NC}"

chmod +x scripts/start.sh scripts/stop.sh scripts/restart.sh
echo -e "${GREEN}✓ Scripts are now executable${NC}\n"

echo -e "${YELLOW}Step 6: Checking installation...${NC}"

# Verify the CLI is accessible
if dropbox-mirror --help &> /dev/null; then
    echo -e "${GREEN}✓ CLI tool is ready${NC}"
else
    echo -e "${YELLOW}⚠ Warning: CLI tool check failed (may need to deactivate/reactivate venv)${NC}"
fi

echo -e "\n${GREEN}=== Installation Complete ===${NC}\n"

echo -e "${YELLOW}Next steps:${NC}"
echo "1. Activate the virtual environment (if not already activated):"
echo -e "   ${GREEN}source .venv/bin/activate${NC}"
echo ""
echo "2. Copy and edit the configuration file:"
echo -e "   ${GREEN}cp config.example.ini ~/.dropbox-mirror/config.ini${NC}"
echo -e "   ${GREEN}nano ~/.dropbox-mirror/config.ini${NC}"
echo ""
echo "3. Authorize with Dropbox (first time only):"
echo -e "   ${GREEN}dropbox-mirror auth${NC}"
echo ""
echo "4. Start the application:"
echo -e "   ${GREEN}./scripts/start.sh${NC}"
echo ""
echo "For more information, see README.md"
