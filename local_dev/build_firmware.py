"""Precompile ESP8266 modules on the host to avoid device compiler RAM spikes."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=root / 'firmware' / 'micropython' / 'build')
    args = parser.parse_args()
    try:
        subprocess.run([sys.executable, '-m', 'mpy_cross', '--version'], check=True)
    except subprocess.CalledProcessError:
        parser.exit(1, 'Install mpy-cross matching the board MicroPython version first.\n')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for source in sorted((root / 'firmware' / 'micropython' / 'src').glob('*.py')):
        if source.name in ('main.py', 'boot.py'):
            # MicroPython starts these entrypoints as .py files.
            shutil.copyfile(source, output / source.name)
        else:
            subprocess.run([sys.executable, '-m', 'mpy_cross', '-O2',
                            '-s', source.name, '-o', str(output / (source.stem + '.mpy')),
                            str(source)], check=True)
    print('Firmware built in {}'.format(output))
    print('Private .settings is not copied; upload it separately from firmware/.settings.')


if __name__ == '__main__':
    main()
