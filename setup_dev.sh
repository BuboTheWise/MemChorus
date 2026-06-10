#!/bin/bash
# MemChorus Development Setup Script

echo "Setting up MemChorus development environment..."

# Create necessary directories if they don't exist
mkdir -p /home/bubo/.hermes/workspace/Code/MemChorus/{scripts,tests,docs}

# Copy main files to the proper locations
cp memchorus.py /home/bubo/.hermes/workspace/Code/MemChorus/
cp SKILL.md /home/bubo/.hermes/workspace/Code/MemChorus/
cp README.md /home/bubo/.hermes/workspace/Code/MemChorus/
cp TESTING.md /home/bubo/.hermes/workspace/Code/MemChorus/
cp example_usage.py /home/bubo/.hermes/workspace/Code/MemChorus/

echo "MemChorus development environment setup complete!"
echo ""
echo "Files created:"
ls -la /home/bubo/.hermes/workspace/Code/MemChorus/
echo ""
echo "To run tests:"
echo "  cd /home/bubo/.hermes/workspace/Code/MemChorus"
echo "  python test_memchorus.py"
echo ""
echo "To see example usage:"
echo "  python example_usage.py"