from setuptools import setup, find_packages

setup(
    name="memchorus",
    version="1.0.0",
    author="BuboTheWise",
    author_email="bubo@wisdom.systems",
    description="Memory orchestration system for Hermes agents",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/BuboTheWise/MemChorus",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        # No external dependencies for now
    ],
)