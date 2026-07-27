//! Split a simple-query batch into its individual statements.
//!
//! The PostgreSQL simple query protocol lets one Query message carry several
//! statements separated by semicolons, and the server is expected to run each
//! in turn. Clients that build their SQL themselves rely on this: Npgsql sends
//! `SELECT version();` followed by its type-loading queries as a single message
//! when a connection opens, so a server that reads only the first statement
//! cannot be connected to at all. pgjdbc and psqlodbc happen to split
//! client-side, which is why this only shows up with some drivers.
//!
//! Splitting is lexical rather than parsed: the original statement text is
//! returned untouched, because the catalog emulation rewrites SQL as text and
//! round-tripping it through a parser would change what those rewrites see.
//!
//! A plain `split(';')` would corrupt any statement containing a semicolon
//! inside a string, a quoted identifier, or a comment, so this tracks the
//! lexical states where a semicolon is data rather than a separator.

/// Split a simple-query batch into its non-empty statements.
///
/// Semicolons inside single-quoted strings, escape strings, double-quoted
/// identifiers, dollar-quoted bodies, line comments, and block comments are
/// part of the statement rather than separators. Statements that are empty or
/// contain only whitespace are dropped, so a trailing semicolon (or a batch of
/// nothing but semicolons) does not produce blank statements.
///
/// Returns slices borrowed from `sql`, each trimmed of surrounding whitespace.
pub fn split_statements(sql: &str) -> Vec<&str> {
    let bytes = sql.as_bytes();
    let mut statements = Vec::new();
    let mut start = 0;
    let mut index = 0;

    while index < bytes.len() {
        match bytes[index] {
            b'\'' => index = skip_quoted(bytes, index, b'\'', is_escape_string(bytes, index)),
            b'"' => index = skip_quoted(bytes, index, b'"', false),
            b'$' => match skip_dollar_quoted(bytes, index) {
                Some(end) => index = end,
                None => index += 1,
            },
            b'-' if bytes.get(index + 1) == Some(&b'-') => index = skip_line_comment(bytes, index),
            b'/' if bytes.get(index + 1) == Some(&b'*') => index = skip_block_comment(bytes, index),
            b';' => {
                push_statement(&mut statements, &sql[start..index]);
                index += 1;
                start = index;
            }
            _ => index += 1,
        }
    }

    push_statement(&mut statements, &sql[start..]);
    statements
}

/// Add a statement to the batch unless it is blank.
fn push_statement<'a>(statements: &mut Vec<&'a str>, candidate: &'a str) {
    let trimmed = candidate.trim();
    if !trimmed.is_empty() {
        statements.push(trimmed);
    }
}

/// Report whether the quote at `index` opens an `E'...'` escape string.
///
/// In an escape string a backslash escapes the following character, including
/// a quote, so the closing quote has to be found differently than for a regular
/// string. The `E` must be its own token rather than the tail of an identifier,
/// so a preceding word character (as in `dave'...'`) rules it out.
fn is_escape_string(bytes: &[u8], index: usize) -> bool {
    if index == 0 {
        return false;
    }
    if !matches!(bytes[index - 1], b'e' | b'E') {
        return false;
    }
    match index.checked_sub(2).map(|i| bytes[i]) {
        None => true,
        Some(previous) => !is_word_byte(previous),
    }
}

/// Report whether a byte can appear inside an unquoted identifier.
fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'$' || byte >= 0x80
}

/// Return the index just past a quoted run that began at `index`.
///
/// Handles the doubled-quote escape both kinds of quoting use (`''` and `""`),
/// and backslash escapes when `backslash_escapes` is set. An unterminated run
/// consumes the rest of the input: the statement is malformed either way, and
/// letting the database report it gives a better error than splitting it into
/// fragments here.
fn skip_quoted(bytes: &[u8], index: usize, quote: u8, backslash_escapes: bool) -> usize {
    let mut position = index + 1;
    while position < bytes.len() {
        let byte = bytes[position];
        if backslash_escapes && byte == b'\\' {
            position += 2;
            continue;
        }
        if byte == quote {
            if bytes.get(position + 1) == Some(&quote) {
                position += 2;
                continue;
            }
            return position + 1;
        }
        position += 1;
    }
    bytes.len()
}

/// Return the index just past a dollar-quoted body starting at `index`.
///
/// Returns None when the `$` does not open one, which is the common case: `$1`
/// and the like are parameter placeholders, not quoting.
fn skip_dollar_quoted(bytes: &[u8], index: usize) -> Option<usize> {
    let tag_end = read_dollar_tag(bytes, index)?;
    let tag = &bytes[index..tag_end];

    let mut position = tag_end;
    while position + tag.len() <= bytes.len() {
        if &bytes[position..position + tag.len()] == tag {
            return Some(position + tag.len());
        }
        position += 1;
    }
    // Unterminated: treat the rest as the body, and let the database report it.
    Some(bytes.len())
}

/// Return the index just past a `$tag$` opener, or None if there is not one.
fn read_dollar_tag(bytes: &[u8], index: usize) -> Option<usize> {
    let mut position = index + 1;
    while position < bytes.len() {
        match bytes[position] {
            b'$' => return Some(position + 1),
            // A tag is an identifier and cannot start with a digit; that rules
            // out `$1`-style placeholders.
            byte if byte.is_ascii_digit() && position == index + 1 => return None,
            byte if is_word_byte(byte) && byte != b'$' => position += 1,
            _ => return None,
        }
    }
    None
}

/// Return the index just past a `--` comment, which runs to end of line.
fn skip_line_comment(bytes: &[u8], index: usize) -> usize {
    let mut position = index + 2;
    while position < bytes.len() && bytes[position] != b'\n' {
        position += 1;
    }
    position
}

/// Return the index just past a block comment, honouring PostgreSQL nesting.
///
/// PostgreSQL nests `/* */`, so an inner comment does not end the outer one.
fn skip_block_comment(bytes: &[u8], index: usize) -> usize {
    let mut position = index + 2;
    let mut depth = 1;
    while position < bytes.len() {
        if bytes[position] == b'/' && bytes.get(position + 1) == Some(&b'*') {
            depth += 1;
            position += 2;
        } else if bytes[position] == b'*' && bytes.get(position + 1) == Some(&b'/') {
            depth -= 1;
            position += 2;
            if depth == 0 {
                return position;
            }
        } else {
            position += 1;
        }
    }
    bytes.len()
}

#[cfg(test)]
mod tests {
    use super::split_statements;

    #[test]
    fn single_statement_is_returned_whole() {
        assert_eq!(split_statements("SELECT 1"), vec!["SELECT 1"]);
    }

    #[test]
    fn statements_are_split_on_semicolons() {
        assert_eq!(
            split_statements("SELECT 1; SELECT 2"),
            vec!["SELECT 1", "SELECT 2"]
        );
    }

    #[test]
    fn npgsql_startup_batch_splits_into_its_statements() {
        // The shape that made Npgsql unable to connect: a version probe
        // followed by a catalog query in one message.
        let batch = "SELECT version(); SELECT ns.nspname FROM pg_namespace AS ns";
        assert_eq!(
            split_statements(batch),
            vec![
                "SELECT version()",
                "SELECT ns.nspname FROM pg_namespace AS ns"
            ]
        );
    }

    #[test]
    fn trailing_semicolon_yields_no_empty_statement() {
        assert_eq!(split_statements("SELECT 1;"), vec!["SELECT 1"]);
    }

    #[test]
    fn blank_and_semicolon_only_input_yields_nothing() {
        assert!(split_statements("").is_empty());
        assert!(split_statements("   ").is_empty());
        assert!(split_statements(";;;").is_empty());
        assert!(split_statements("; ; ").is_empty());
    }

    #[test]
    fn statements_are_trimmed() {
        assert_eq!(
            split_statements("  SELECT 1  ;\n\tSELECT 2\n"),
            vec!["SELECT 1", "SELECT 2"]
        );
    }

    #[test]
    fn semicolon_inside_a_string_does_not_split() {
        assert_eq!(
            split_statements("SELECT 'a;b'; SELECT 2"),
            vec!["SELECT 'a;b'", "SELECT 2"]
        );
    }

    #[test]
    fn doubled_quote_inside_a_string_does_not_end_it() {
        assert_eq!(
            split_statements("SELECT 'it''s; fine'; SELECT 2"),
            vec!["SELECT 'it''s; fine'", "SELECT 2"]
        );
    }

    #[test]
    fn backslash_escape_in_an_escape_string_does_not_end_it() {
        assert_eq!(
            split_statements(r"SELECT E'a\'; b'; SELECT 2"),
            vec![r"SELECT E'a\'; b'", "SELECT 2"]
        );
    }

    #[test]
    fn backslash_is_literal_in_a_regular_string() {
        // Without standard_conforming_strings off, a backslash does not escape
        // in a plain string, so this string ends at the second quote.
        assert_eq!(
            split_statements(r"SELECT 'a\'; SELECT 2"),
            vec![r"SELECT 'a\'", "SELECT 2"]
        );
    }

    #[test]
    fn identifier_ending_in_e_does_not_start_an_escape_string() {
        // "table'" must not be read as an E-string just because the preceding
        // character is an e.
        assert_eq!(
            split_statements("SELECT date'2020-01-01'; SELECT 2"),
            vec!["SELECT date'2020-01-01'", "SELECT 2"]
        );
    }

    #[test]
    fn semicolon_inside_a_quoted_identifier_does_not_split() {
        assert_eq!(
            split_statements("SELECT \"odd;name\" FROM t; SELECT 2"),
            vec!["SELECT \"odd;name\" FROM t", "SELECT 2"]
        );
    }

    #[test]
    fn semicolon_inside_a_line_comment_does_not_split() {
        assert_eq!(
            split_statements("SELECT 1 -- one; two\n; SELECT 2"),
            vec!["SELECT 1 -- one; two", "SELECT 2"]
        );
    }

    #[test]
    fn semicolon_inside_a_block_comment_does_not_split() {
        assert_eq!(
            split_statements("SELECT /* a; b */ 1; SELECT 2"),
            vec!["SELECT /* a; b */ 1", "SELECT 2"]
        );
    }

    #[test]
    fn nested_block_comments_end_at_the_outer_close() {
        assert_eq!(
            split_statements("SELECT /* a /* b; */ c */ 1; SELECT 2"),
            vec!["SELECT /* a /* b; */ c */ 1", "SELECT 2"]
        );
    }

    #[test]
    fn semicolon_inside_a_dollar_quoted_body_does_not_split() {
        assert_eq!(
            split_statements("SELECT $$a; b$$; SELECT 2"),
            vec!["SELECT $$a; b$$", "SELECT 2"]
        );
    }

    #[test]
    fn tagged_dollar_quoting_does_not_split() {
        assert_eq!(
            split_statements("SELECT $tag$a; b$tag$; SELECT 2"),
            vec!["SELECT $tag$a; b$tag$", "SELECT 2"]
        );
    }

    #[test]
    fn parameter_placeholders_are_not_dollar_quotes() {
        // `$1` must stay a placeholder, or everything after it would be read as
        // one quoted body and the batch would never split.
        assert_eq!(
            split_statements("SELECT $1; SELECT $2"),
            vec!["SELECT $1", "SELECT $2"]
        );
    }

    #[test]
    fn unterminated_string_consumes_the_rest() {
        // Malformed either way; keeping it whole lets the database report a
        // better error than fragments would.
        assert_eq!(
            split_statements("SELECT 'unclosed; SELECT 2"),
            vec!["SELECT 'unclosed; SELECT 2"]
        );
    }

    #[test]
    fn many_statements_all_split() {
        assert_eq!(
            split_statements("SELECT 1;SELECT 2;SELECT 3;SELECT 4"),
            vec!["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4"]
        );
    }
}
