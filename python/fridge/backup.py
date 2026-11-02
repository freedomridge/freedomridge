"""Freedom Ridge database backup and restore utilities.

Usage:
    # Full backup
    python -m fridge.backup --backup --output freedomridge_backup.sql

    # Restore from backup
    python -m fridge.backup --restore --input freedomridge_backup.sql

    # Transfer ONE dataset to another Freedom Ridge instance (samples, metadata,
    # embeddings, hashes — remapped onto a fresh dataset_id). The --export-dataset/
    # --import-dataset flags are unsupported (they dumped ALL datasets / collided on
    # primary keys); use the API-level transfer tool instead:
    python -m fridge.sync_between_instances \
        --source <source-api-url> --target <target-api-url> --dataset large-dataset
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path


def get_db_params():
    """Get database connection parameters from environment or defaults."""
    db_url = os.environ.get("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")
    # Parse postgres://user:pass@host:port/dbname
    url = db_url.replace("postgres://", "").replace("postgresql://", "")
    userpass, hostdb = url.split("@")
    user, password = userpass.split(":")
    hostport, dbname = hostdb.split("/")
    host, port = hostport.split(":") if ":" in hostport else (hostport, "5432")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "dbname": dbname,
    }


def backup(output_path: str):
    """Full database backup using pg_dump."""
    params = get_db_params()
    env = {**os.environ, "PGPASSWORD": params["password"]}

    cmd = [
        "pg_dump",
        "-h",
        params["host"],
        "-p",
        params["port"],
        "-U",
        params["user"],
        "-d",
        params["dbname"],
        "--no-owner",
        "--no-acl",
        # Keep dumps self-contained. Restore still requires an empty target: --clean cannot
        # remove foreign keys belonging to newer tables absent from the backup.
        "--clean",
        "--if-exists",
        "-f",
        output_path,
    ]

    print(f"Backing up database to {output_path}...")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Backup failed: {result.stderr}")
        sys.exit(1)
    size = os.path.getsize(output_path) / (1024 * 1024)
    print(f"Backup complete: {output_path} ({size:.1f} MB)")


def restore(input_path: str):
    """Restore into an empty database, ANALYZE, then apply this checkout's migrations."""
    root = Path(__file__).resolve().parents[2]
    migrations = root / "scripts" / "apply_migrations.sh"
    if not migrations.is_file():
        raise RuntimeError("Restore requires a Freedom Ridge source checkout containing scripts/apply_migrations.sh")
    # Fail before touching the database if the input is missing/unreadable.
    with open(input_path, "rb"):
        pass
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("Restore requires an explicit DATABASE_URL for the empty target")
    if not shutil.which("psql"):
        raise RuntimeError("Restore requires a matching PostgreSQL psql client on PATH")
    env = {**os.environ, "FRIDGE_PYTHON": sys.executable}

    cmd = [
        sys.executable,
        str(root / "scripts" / "psql_url.py"),
        "-X",
        # Stop on the FIRST SQL error and exit nonzero — otherwise psql runs to the end and exits 0
        # even after failed statements, letting a partial/failed restore report success.
        "-v",
        "ON_ERROR_STOP=1",
    ]

    # Extension-owned tables (e.g. spatial_ref_sys) are allowed. Check all user schemas,
    # not just public: --clean only knows objects that existed when the backup was made.
    check = subprocess.run(
        [
            *cmd,
            "-Atc",
            """SELECT EXISTS (
                   SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                     AND n.nspname NOT LIKE 'pg_toast%'
                     AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
                     AND NOT EXISTS (
                         SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                         AND d.objid = c.oid AND d.deptype = 'e'
                     )
               )""",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        raise RuntimeError(f"Restore FAILED checking target: {check.stderr.strip()}")
    if check.stdout.strip() != "f":
        raise RuntimeError(
            "Restore requires an EMPTY database (extensions only), not schema.sql. "
            "Stop services and explicitly drop/recreate the target database, then retry. "
            "See docs/BACKUP_AND_MIGRATION.md.",
        )

    print(f"Restoring database from {input_path}...", flush=True)
    # scripts/backup_loop.sh (the scheduled sidecar, and therefore the file an operator actually has
    # in a real recovery) writes .sql.gz, so accept it directly rather than making them gunzip to a
    # temp file the size of the whole uncompressed dump. Plain .sql still uses -f.
    if input_path.endswith(".gz"):
        # A GzipFile canNOT be handed to subprocess as stdin: its fileno() is the COMPRESSED file's
        # descriptor, so psql would be fed gzip bytes. Stream the decompressed data in ourselves.
        # stdout is discarded (a restore prints a line per statement); ON_ERROR_STOP=1 makes psql
        # exit on the first failure, so stderr stays small and this can't deadlock on a full pipe.
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stderr is not None
        try:
            with gzip.open(input_path, "rb") as fh:
                shutil.copyfileobj(fh, proc.stdin)
        except BrokenPipeError:
            pass  # psql already exited (ON_ERROR_STOP) — its stderr below says why
        finally:
            proc.stdin.close()
        # Read stderr AFTER stdin is closed rather than using communicate() (which would try to
        # flush the already-closed stdin). Safe from a pipe-buffer deadlock: stdout is discarded,
        # and ON_ERROR_STOP makes psql exit at the first error, so stderr stays tiny.
        stderr_text = proc.stderr.read().decode(errors="replace")
        proc.stderr.close()
        returncode = proc.wait()
    else:
        result = subprocess.run([*cmd, "-f", input_path], env=env, capture_output=True, text=True)
        returncode = result.returncode
        stderr_text = result.stderr

    if returncode != 0:
        # A failed/partial restore MUST NOT report success — this is the never-lose-data path.
        raise RuntimeError(
            f"Restore FAILED (psql exit {returncode}) from {input_path}:\n{stderr_text.strip()}",
        )
    subprocess.run([*cmd, "-c", "ANALYZE"], env=env, check=True)
    subprocess.run(["bash", str(migrations), "--database-url-env"], cwd=root, env=env, check=True)
    print("Restore and migrations complete.")


def _single_dataset_unsupported(dataset_name: str = "") -> None:
    """Single-dataset export/import via this module is intentionally unsupported.

    The previous pg_dump implementation dumped EVERY dataset's rows (there is no
    per-dataset filter in a --data-only dump of the shared tables), and importing
    them with `psql -f` reinserted the original primary keys, colliding on the
    target. Both were silent footguns.

    For portable, correct transfer of ONE dataset — samples, metadata, embeddings,
    and hashes, remapped onto a fresh dataset_id — use the SDK transfer tool, which
    talks to each instance's API and add_samples()es with on_duplicate='skip':

        python -m fridge.sync_between_instances \\
            --source <source-api-url> --target <target-api-url> --dataset NAME
    """
    print("Single-dataset export/import via fridge.backup is not supported.")
    print("pg_dump here would dump ALL datasets, and the import would collide on")
    print("primary keys. Use instance-to-instance transfer instead:\n")
    print("  python -m fridge.sync_between_instances \\")
    print(f"      --source <source-api-url> --target <target-api-url> --dataset {dataset_name or 'NAME'}\n")
    print("Or take/restore a FULL database dump with --backup / --restore.")
    sys.exit(2)


def export_dataset(dataset_name: str, output_path: str):
    """Deprecated/unsupported — see _single_dataset_unsupported."""
    _single_dataset_unsupported(dataset_name)


def main():
    parser = argparse.ArgumentParser(description="Freedom Ridge backup and restore")
    parser.add_argument("--backup", action="store_true", help="Full database backup")
    parser.add_argument("--restore", action="store_true", help="Restore from backup")
    parser.add_argument("--export-dataset", type=str, help="Export a single dataset")
    parser.add_argument("--import-dataset", action="store_true", help="Import a dataset")
    parser.add_argument("--output", "-o", type=str, help="Output file path")
    parser.add_argument("--input", "-i", type=str, help="Input file path")
    args = parser.parse_args()

    if args.backup:
        output = args.output or "freedomridge_backup.sql"
        backup(output)
    elif args.restore:
        if not args.input:
            parser.error("--input required for restore")
        try:
            restore(args.input)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            parser.exit(1, f"{error}\n")
    elif args.export_dataset:
        output = args.output or f"{args.export_dataset}_export.sql"
        export_dataset(args.export_dataset, output)
    elif args.import_dataset:
        _single_dataset_unsupported()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
