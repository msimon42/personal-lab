"""Package advisory schema bytes from their single maintained source directory."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        # Reused setuptools build trees otherwise retain deleted transports in
        # new wheels. Only clear this package's generated output, never source.
        source_package = (Path(__file__).parent / "backend").resolve()
        built_package = (Path(self.build_lib) / "backend").absolute()
        resolved = built_package.resolve()
        if resolved == source_package or resolved.is_relative_to(source_package) or source_package.is_relative_to(resolved):
            raise ValueError("Build output must be separate from maintained source files.")
        if built_package.is_symlink():
            raise ValueError("Build output cannot be a linked package directory.")
        if built_package.exists():
            shutil.rmtree(built_package)
        super().run()
        source = Path(__file__).parent / "schemas" / "ck3"
        files = sorted(source.glob("*.json"))
        if not files or not (source / "_manifest.json").is_file():
            raise ValueError("The CK3 advisory schemas are missing from the source distribution.")
        target = Path(self.build_lib) / "backend" / "engine" / "data" / "ck3"
        # A reused build directory must not retain schemas removed from the source.
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for path in files:
            shutil.copyfile(path, target / path.name)


setup(cmdclass={"build_py": BuildPy})
