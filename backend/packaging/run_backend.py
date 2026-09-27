"""Entry point of the packaged backend (PyInstaller); ``python -m jarvis`` in development."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()  # libraries that start worker processes re-enter here
    from jarvis.__main__ import main

    main()
