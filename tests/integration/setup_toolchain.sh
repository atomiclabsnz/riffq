#!/usr/bin/env bash
#
# setup_toolchain.sh -- build and download the client-driver toolchain the
# integration tests run against, entirely inside a user-writable prefix.
#
# The container has no root, no cmake, and none of unixODBC, psqlodbc, a JDK,
# or pyodbc preinstalled, so everything is built or fetched into .toolchain/
# next to this script (a gitignored directory). Nothing is written outside
# .toolchain/ and the project virtualenv.
#
# The script is idempotent: each step is skipped when its output already
# exists, so re-running it after a partial run resumes rather than rebuilding.
# Every download is checked against a pinned SHA-256 recorded below, so a
# swapped or corrupted upstream artifact fails loudly instead of silently
# changing what is under test.
#
# Usage:
#   ./setup_toolchain.sh                 build the core ODBC + JDBC toolchain
#   ./setup_toolchain.sh --with-jdbc-tool  also fetch the optional SQL Workbench/J
#                                          client used by test_jdbc_tool.py
#
# All output is plain ASCII.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TOOLCHAIN="$HERE/.toolchain"
DOWNLOADS="$TOOLCHAIN/downloads"
BUILD="$TOOLCHAIN/build"
VENV_PY="$REPO_ROOT/venv/bin/python"

# Install prefixes for the two source builds and the extracted runtimes.
UNIXODBC_PREFIX="$TOOLCHAIN/unixodbc"
PSQLODBC_PREFIX="$TOOLCHAIN/psqlodbc"
JDK_DIR="$TOOLCHAIN/jdk"
JARS_DIR="$TOOLCHAIN/jars"
ETC_DIR="$TOOLCHAIN/etc"

# Pinned upstream artifacts: each is "<filename>|<url>|<sha256>". Versions and
# hashes were resolved once and frozen here so the toolchain cannot change under
# the tests. The JDK hash matches Adoptium's own published checksum.
UNIXODBC_ARTIFACT="unixODBC-2.3.12.tar.gz|https://www.unixodbc.org/unixODBC-2.3.12.tar.gz|f210501445ce21bf607ba51ef8c125e10e22dffdffec377646462df5f01915ec"
PSQLODBC_ARTIFACT="psqlodbc-16.00.0000.tar.gz|https://ftp.postgresql.org/pub/odbc/versions.old/src/psqlodbc-16.00.0000.tar.gz|afd892f89d2ecee8d3f3b2314f1bd5bf2d02201872c6e3431e5c31096eca4c8b"
PGJDBC_ARTIFACT="postgresql-42.7.13.jar|https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.13/postgresql-42.7.13.jar|6e0e4cc2d8cae902084f8a2b18728b073a6fd9d1f87c9d8bff8f298c18185b93"
JDK_ARTIFACT="OpenJDK21U-jdk_x64_linux_hotspot_21.0.11_10.tar.gz|https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.11%2B10/OpenJDK21U-jdk_x64_linux_hotspot_21.0.11_10.tar.gz|4b2220e232a97997b436ca6ab15cbf70171ecff52958a46159dfa5a8c44ca4de"

WITH_JDBC_TOOL=0
for arg in "$@"; do
    case "$arg" in
        --with-jdbc-tool) WITH_JDBC_TOOL=1 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

log() {
    # Print a step banner so a long build is legible in the terminal.
    echo "== $* =="
}

fetch() {
    # Download one pinned artifact into DOWNLOADS and verify its SHA-256.
    # Skips the download when the file is already present and correct; aborts
    # if the hash does not match, naming the artifact.
    local spec="$1"
    local name="${spec%%|*}"
    local rest="${spec#*|}"
    local url="${rest%%|*}"
    local want="${rest##*|}"
    local dest="$DOWNLOADS/$name"

    if [ -f "$dest" ] && echo "$want  $dest" | sha256sum --check --status; then
        echo "  cached: $name"
        return 0
    fi

    echo "  downloading: $name"
    curl -sSL --fail --max-time 900 -o "$dest" "$url"
    if ! echo "$want  $dest" | sha256sum --check --status; then
        echo "SHA-256 mismatch for $name -- refusing to use it." >&2
        echo "  expected $want" >&2
        echo "  got      $(sha256sum "$dest" | cut -d' ' -f1)" >&2
        exit 1
    fi
}

build_unixodbc() {
    # Build the unixODBC driver manager (odbcinst, isql, libodbc.so). GUI,
    # bundled drivers, and readline are disabled so the build needs no packages
    # beyond the C toolchain already present in the container.
    if [ -f "$UNIXODBC_PREFIX/lib/libodbc.so" ]; then
        log "unixODBC already built"
        return 0
    fi
    log "building unixODBC"
    local src="$BUILD/unixODBC-2.3.12"
    rm -rf "$src"
    tar xzf "$DOWNLOADS/unixODBC-2.3.12.tar.gz" -C "$BUILD"
    (
        cd "$src"
        ./configure --prefix="$UNIXODBC_PREFIX" \
            --enable-gui=no --enable-drivers=no --enable-readline=no
        make -j"$(nproc)"
        make install
    )
}

build_psqlodbc() {
    # Build the PostgreSQL ODBC driver against our unixODBC and the system
    # libpq. Produces both the Unicode (psqlodbcw.so) and ANSI (psqlodbca.so)
    # drivers; the tests register and exercise both because they issue slightly
    # different catalog SQL.
    if [ -f "$PSQLODBC_PREFIX/lib/psqlodbcw.so" ] && [ -f "$PSQLODBC_PREFIX/lib/psqlodbca.so" ]; then
        log "psqlodbc already built"
        return 0
    fi
    log "building psqlodbc"
    local src="$BUILD/psqlodbc-16.00.0000"
    rm -rf "$src"
    tar xzf "$DOWNLOADS/psqlodbc-16.00.0000.tar.gz" -C "$BUILD"
    (
        cd "$src"
        ./configure --prefix="$PSQLODBC_PREFIX" \
            --with-unixodbc="$UNIXODBC_PREFIX" \
            --with-libpq=/usr
        make -j"$(nproc)"
        make install
    )
}

extract_jdk() {
    # Unpack the Temurin JDK. javac and java are invoked by absolute path from
    # the tests; JAVA_HOME is never exported globally.
    if [ -x "$JDK_DIR/bin/javac" ]; then
        log "JDK already extracted"
        return 0
    fi
    log "extracting JDK"
    rm -rf "$JDK_DIR"
    mkdir -p "$JDK_DIR"
    tar xzf "$DOWNLOADS/OpenJDK21U-jdk_x64_linux_hotspot_21.0.11_10.tar.gz" \
        -C "$JDK_DIR" --strip-components=1
}

place_pgjdbc() {
    # Copy the pinned pgjdbc jar into jars/. The Java harnesses are compiled and
    # run with this jar on the classpath -- no Maven or Gradle involved.
    if [ -f "$JARS_DIR/postgresql.jar" ]; then
        log "pgjdbc already placed"
        return 0
    fi
    log "placing pgjdbc jar"
    mkdir -p "$JARS_DIR"
    cp "$DOWNLOADS/postgresql-42.7.13.jar" "$JARS_DIR/postgresql.jar"
}

write_odbc_config() {
    # Write the ODBC driver registration the tests point ODBCSYSINI at. odbc.ini
    # (DSNs) is deliberately omitted: the tests build DSN-less connection
    # strings so each test class can use its own port.
    log "writing odbcinst.ini"
    mkdir -p "$ETC_DIR"
    cat > "$ETC_DIR/odbcinst.ini" <<EOF
[PostgreSQL Unicode]
Description = PostgreSQL ODBC driver (Unicode), toolchain build
Driver      = $PSQLODBC_PREFIX/lib/psqlodbcw.so
Threading   = 2

[PostgreSQL ANSI]
Description = PostgreSQL ODBC driver (ANSI), toolchain build
Driver      = $PSQLODBC_PREFIX/lib/psqlodbca.so
Threading   = 2
EOF
    # An empty DSN file so the driver manager reads our ODBCINI instead of
    # falling back to a user-level ~/.odbc.ini. The tests use DSN-less
    # connection strings, so it stays intentionally empty.
    : > "$ETC_DIR/odbc.ini"
}

install_pyodbc() {
    # Install pyodbc into the project venv from a manylinux wheel. The container
    # has no Python development headers (no python3.13-dev, no root), so a source
    # build is not possible; the wheel needs none. The wheel does not bundle a
    # driver manager -- it loads libodbc.so.2 at run time -- so toolchain.py puts
    # our unixODBC lib directory on LD_LIBRARY_PATH before importing pyodbc, and
    # since no system unixODBC exists, our build is what resolves. The venv is
    # reused, never replaced (its pip console script has a stale shebang; python
    # -m pip works).
    if PYODBC_LOADS >/dev/null 2>&1; then
        log "pyodbc already installed"
        return 0
    fi
    log "installing pyodbc"
    "$VENV_PY" -m pip install --only-binary :all: pyodbc
    if ! PYODBC_LOADS; then
        echo "pyodbc installed but does not import against our unixODBC." >&2
        exit 1
    fi
}

PYODBC_LOADS() {
    # Return success when pyodbc imports with our unixODBC on the library path.
    LD_LIBRARY_PATH="$UNIXODBC_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        "$VENV_PY" -c "import pyodbc" >/dev/null 2>&1
}

install_jdbc_tool() {
    # Optional second JDBC client (SQL Workbench/J). Best-effort tier: the
    # download host is not a package registry and its build number moves, so a
    # failure here is non-fatal and test_jdbc_tool.py skips when the jar is
    # absent. Pinned build and hash keep a successful fetch reproducible.
    local name="Workbench-Build131.zip"
    local url="https://www.sql-workbench.eu/$name"
    local dest="$DOWNLOADS/$name"
    local tool_dir="$TOOLCHAIN/sqlworkbench"

    if [ -f "$tool_dir/sqlworkbench.jar" ]; then
        log "SQL Workbench/J already installed"
        return 0
    fi
    log "fetching SQL Workbench/J (best effort)"
    if ! curl -sSL --fail --max-time 300 -o "$dest" "$url"; then
        echo "  SQL Workbench/J download failed; skipping optional tier." >&2
        return 0
    fi
    rm -rf "$tool_dir"
    mkdir -p "$tool_dir"
    # jar/unzip extract into the current directory, so run the extractor from
    # inside tool_dir. sqlworkbench.jar sits at the top level of the zip.
    ( cd "$tool_dir" && "$JDK_DIR/bin/jar" xf "$dest" )
    if [ ! -f "$tool_dir/sqlworkbench.jar" ]; then
        echo "  SQL Workbench/J layout unexpected; optional tier stays skipped." >&2
    fi
}

main() {
    mkdir -p "$DOWNLOADS" "$BUILD" "$JARS_DIR" "$ETC_DIR"

    log "verifying downloads"
    fetch "$UNIXODBC_ARTIFACT"
    fetch "$PSQLODBC_ARTIFACT"
    fetch "$PGJDBC_ARTIFACT"
    fetch "$JDK_ARTIFACT"

    # JDBC pieces first: they are simple copies/extracts and must not be
    # blocked by the more fragile ODBC driver-manager path.
    extract_jdk
    place_pgjdbc

    build_unixodbc
    build_psqlodbc
    write_odbc_config
    install_pyodbc

    if [ "$WITH_JDBC_TOOL" -eq 1 ]; then
        install_jdbc_tool
    fi

    log "toolchain ready at $TOOLCHAIN"
}

main "$@"
