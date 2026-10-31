# Entry point of the frozen core (PyInstaller): the same CLI as `benchscout`.
import multiprocessing

from benchscout.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
