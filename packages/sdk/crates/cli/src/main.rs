//! Host administration shares its implementation with the installed Python entrypoint.
use mu_adapter::Provider;
#[tokio::main]
async fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.is_empty() || args == ["--version"] || args == ["info"] {
        println!(
            "{}",
            serde_json::json!({"sdk_api_version":mu_protocol::SDK_API_VERSION,"protocol_major":mu_protocol::PROTOCOL_MAJOR,"runtime":"embedded","adapters":["android","macos","browser","linux"],"bindings":["python","javascript"]})
        );
        return;
    }
    let result = if args.get(1).is_some_and(|a| a == "linux")
        && args.first().is_some_and(|a| a == "setup" || a == "doctor")
    {
        let option = |key: &str| {
            args.iter()
                .position(|a| a == key)
                .and_then(|i| args.get(i + 1))
        };
        mu_host::linux::admin(&serde_json::json!({"operation":args[0],"display":option("--display"),"xauthority":option("--xauthority"),"python":option("--python")})).await
    } else if args.get(1).is_some_and(|a| a == "browser")
        && args.first().is_some_and(|a| a == "setup" || a == "doctor")
    {
        let engine = args
            .iter()
            .position(|a| a == "--engine")
            .and_then(|i| args.get(i + 1))
            .map(String::as_str)
            .unwrap_or("all");
        mu_host::browser_admin::admin(&args[0], engine).await
    } else if args.first().is_some_and(|a| a == "setup")
        && args.get(1).is_some_and(|a| a == "macos")
    {
        let replace = args.iter().any(|a| a == "--replace");
        let archive = args
            .iter()
            .position(|a| a == "--archive")
            .and_then(|i| args.get(i + 1))
            .map(String::as_str);
        mu_adapter_macos::setup::install(replace, archive)
    } else if args == ["doctor", "macos"] {
        match mu_adapter_macos::MacosProvider::connect().await {
            Ok(client) => {
                let result = client.doctor().await;
                let _ = client.close().await;
                result
            }
            Err(e) => Err(e),
        }
    } else {
        eprintln!(
            "usage: mu [setup|doctor linux [--display DISPLAY] [--xauthority PATH] [--python PATH]|info|--version|setup macos [--replace] [--archive PATH]|doctor macos|setup browser [--engine ENGINE]|doctor browser [--engine ENGINE]]"
        );
        std::process::exit(2)
    };
    match result {
        Ok(v) => {
            println!("{v}");
            if v["state"] == "needs_setup" {
                std::process::exit(1);
            }
        }
        Err(e) => {
            println!(
                "{}",
                serde_json::json!({"state":"needs_user_action","error":e.to_string()})
            );
            std::process::exit(1);
        }
    }
}
