@echo off
python setup.py clean
python setup.py sdist
pip install -U .\dist\bzt-1.11.0+3dgiordano.tar.gz