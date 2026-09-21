//! A Rust consumer of riffq, with no Python anywhere.
//!
//! G2's acceptance is that this exists: a binary that depends on the crate as a
//! library, implements the seams, and serves — without linking an interpreter.
//!
//! It is deliberately the smallest thing that proves it. A runner that answers
//! real queries over a real engine is G3 (#41); what matters here is that the
//! protocol core is reachable from outside the `Server` pyclass at all.

use std::collections::HashMap;
use std::sync::Arc;

use async_trait::async_trait;
use bytes::Bytes;
use datafusion::execution::context::SessionContext;
use pgwire::api::Type;
// The lib target is `_riffq` -- the leading underscore is the Python extension
// module convention, and it is what a Rust consumer gets as the extern name.
// Aliased rather than lived with, so the rest of this file reads as it should.
use _riffq as riffq;
use riffq::{BoolCallbackResult, QueryResult, QueryRunner, SessionHooks, serve};

/// Answers everything with a command tag. Enough to prove the seam binds.
struct TagRunner;

#[async_trait]
impl QueryRunner for TagRunner {
    async fn execute(
        &self,
        query: String,
        _params: Option<Vec<Option<Bytes>>>,
        _param_types: Option<Vec<Type>>,
        _do_describe: bool,
        _connection_id: u64,
    ) -> datafusion::error::Result<QueryResult> {
        println!("  query: {query}");
        Ok(QueryResult::Tag("SELECT 0".to_string()))
    }
}

/// Lets everyone in and asks nobody. A deployment would not.
struct OpenDoor;

#[async_trait]
impl SessionHooks for OpenDoor {
    async fn on_connect(
        &self,
        connection_id: u64,
        ip: String,
        port: u16,
        _server_name: Option<&str>,
    ) -> BoolCallbackResult {
        println!("  connect {connection_id} from {ip}:{port}");
        BoolCallbackResult { allowed: true, error: None }
    }

    fn authentication_enabled(&self) -> bool {
        false
    }

    async fn on_authentication(
        &self,
        _connection_id: u64,
        _user: Option<String>,
        _database: Option<String>,
        _host: String,
        _password: String,
    ) -> BoolCallbackResult {
        BoolCallbackResult { allowed: true, error: None }
    }

    async fn on_disconnect(&self, connection_id: u64, _ip: String, _port: u16) {
        println!("  disconnect {connection_id}");
    }
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let addr = std::env::args().nth(1).unwrap_or_else(|| "127.0.0.1:55444".into());

    let ctx = Arc::new(SessionContext::new());
    let mut map: HashMap<String, Arc<SessionContext>> = HashMap::new();
    map.insert("demo".to_string(), ctx.clone());

    println!("serving on {addr} with no interpreter in the process");
    serve(
        &addr,
        None,
        Arc::new(OpenDoor),
        Arc::new(map),
        ctx,
        riffq::SERVER_VERSION.to_string(),
        Arc::new(|_conn_ctx| Arc::new(TagRunner) as Arc<dyn QueryRunner>),
    )
    .await
}
