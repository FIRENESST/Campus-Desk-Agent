use std::env;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let host = env::var("SCHEDULE_HOST").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port = env::var("SCHEDULE_PORT").unwrap_or_else(|_| "19005".to_string());
    let address = format!("{host}:{port}");
    let router = match env::var_os("SCHEDULE_DATA_FILE") {
        Some(path) if !path.is_empty() => {
            use std::io::Read;
            let mut data = Vec::new();
            std::fs::File::open(path)?
                .take(8 * 1024 * 1024 + 1)
                .read_to_end(&mut data)?;
            schedule::app_with_data(&data)?
        }
        _ => schedule::app(),
    };
    let listener = tokio::net::TcpListener::bind(&address).await?;
    println!("课表服务已启动：http://{address}");

    axum::serve(listener, router)
        .with_graceful_shutdown(async {
            tokio::signal::ctrl_c().await.expect("无法监听 Ctrl-C");
        })
        .await?;
    Ok(())
}
