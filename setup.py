import subprocess
from setuptools import setup

with open('requirements.txt') as f:
    reqs = f.read()
reqs = reqs.strip().splitlines()

with open('README.rst') as f:
    long_desc = f.read()

version = subprocess.check_output(
    'git rev-parse --short HEAD', shell=True
).decode('ascii').strip()

setup(
    name='buttonbot',
    version='0.0a' + version,
    description='Play sound effects on Discord',
    url='https://github.com/Kenny2github/buttonbot',
    author='kenny2discord',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Topic :: Communications :: Chat',
        'License :: OSI Approved :: MIT License',
        'Operating System :: Microsoft :: Windows :: Windows 10',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.7'
    ],
    keywords='discord bot bruh nut sound effects',
    py_modules='main.py',
    install_requires=reqs,
    python_requires='>=3.7',
)