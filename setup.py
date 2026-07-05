import os
from setuptools import setup, find_packages


def get_version():
    """Read __version__ from the package __init__ without importing."""
    init_file = os.path.join(os.path.dirname(__file__), "src", "memchorus", "__init__.py")
    with open(init_file, "r") as f:
        for line in f:
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip("\"'")
    return "0.0.0"


def _read_readme():
    """Safely read README.md for the long description."""
    readme_path = os.path.join(os.path.dirname(__file__), "README.md")
    if os.path.isfile(readme_path):
        with open(readme_path, "r") as f:
            return f.read()
    return ""


setup(
    name="memchorus",
    version=get_version(),
    author="BuboTheWise",
    description="Memory orchestration system for Hermes agents",
    long_description=_read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/BuboTheWise/MemChorus",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        "pydantic>=2.0",       # schema_v1 validation
        "pyyaml>=5.4",         # YAML loop definition loader
    ],
)