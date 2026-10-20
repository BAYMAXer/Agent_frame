"""Start the local environment that matches the host OS."""
import sys

from .run_windows_local import main


def host_platform():
    if sys.platform == 'darwin':
        return 'mac'
    if sys.platform == 'win32':
        return 'windows'
    raise RuntimeError(f'本机任务目前支持 macOS 和 Windows，当前平台: {sys.platform}')


if __name__ == '__main__':
    raise SystemExit(main(platform=('linux' if sys.platform.startswith('linux') and '--cli-only' in sys.argv else host_platform())))
