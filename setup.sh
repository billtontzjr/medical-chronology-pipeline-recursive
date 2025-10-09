#!/bin/bash
# Setup script for Medical Chronology Pipeline

echo "================================================"
echo "  Medical Chronology Pipeline - Setup"
echo "================================================"
echo ""

# Check Python version
echo "🔍 Checking Python version..."
python3 --version

if [ $? -ne 0 ]; then
    echo "❌ Python 3 is not installed. Please install Python 3.10 or higher."
    exit 1
fi

# Create virtual environment
echo ""
echo "🐍 Creating virtual environment..."
python3 -m venv venv

# Activate virtual environment
echo "✅ Virtual environment created"
echo ""
echo "📦 Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo ""
echo "⬆️  Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo ""
echo "📚 Installing dependencies..."
pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌ Failed to install dependencies"
    exit 1
fi

echo ""
echo "✅ Dependencies installed successfully"

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo ""
    echo "📝 Creating .env file..."
    cp .env.example .env
    echo "✅ Created .env file from template"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env and add your API keys:"
    echo "   - DROPBOX_ACCESS_TOKEN"
    echo "   - GOOGLE_CLOUD_API_KEY"
    echo "   - ANTHROPIC_API_KEY"
else
    echo ""
    echo "✅ .env file already exists"
fi

# Initialize git if not already
if [ ! -d .git ]; then
    echo ""
    echo "🔧 Initializing git repository..."
    git init
    echo "✅ Git repository initialized"
fi

echo ""
echo "================================================"
echo "  Setup Complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your API keys"
echo "  2. Activate the virtual environment:"
echo "     source venv/bin/activate"
echo "  3. Run the pipeline:"
echo "     python run_pipeline.py"
echo ""
echo "For more information, see README.md"
echo ""
