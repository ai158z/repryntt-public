use repryntt_core::genesis;
use repryntt_core::node::{self, NodeArgs};

#[tokio::main]
async fn main() {
    println!("╔════════════════════════════════════════════════╗");
    println!("║       repryntt-core v0.1.0  —  Rust node      ║");
    println!("╚════════════════════════════════════════════════╝");
    println!();

    // Verify genesis parity with Python
    if !genesis::verify_genesis() {
        eprintln!("❌ GENESIS HASH MISMATCH — Rust/Python incompatible!");
        std::process::exit(1);
    }
    println!("✅ Genesis hash matches Python — chains are compatible");
    println!("   Hash: {}…", &genesis::EXPECTED_GENESIS_HASH[..32]);
    println!();

    // Parse configuration from environment
    let args = NodeArgs::from_env();
    if args.address.trim().is_empty() {
        eprintln!("❌ Missing REPRYNTT_ADDRESS.");
        eprintln!(
            "   Runtime node identity must be this device's wallet, not the genesis creator."
        );
        eprintln!("   Run: repryntt chain install");
        eprintln!("   Or set REPRYNTT_ADDRESS to ~/.repryntt/wallet/node_wallet.json → address");
        std::process::exit(1);
    }
    println!("⚙  Config:");
    println!(
        "   Address:  {}…",
        &args.address[..16.min(args.address.len())]
    );
    println!("   Measured TFLOPS:  {}", args.measured_tflops);
    println!("   Compute share:    {:.0}%", args.compute_share * 100.0);
    println!("   Effective TFLOPS: {}", args.tflops);
    println!("   Data dir: {}", args.data_dir.display());
    println!("   RPC bind: {}", args.rpc_bind);
    println!("   P2P port: {}", args.p2p_port);
    println!("   Mining:   {}", args.mining);
    if args.seeds.is_empty() {
        println!("   Seeds:    (none — solo mode)");
        eprintln!();
        eprintln!(
            "⚠️  WARNING: No seed peers configured. This node will NOT join the global network."
        );
        eprintln!("   To connect to the repryntt blockchain, do one of:");
        eprintln!("     • Set REPRYNTT_BOOTSTRAP_NODES=10.0.0.19:5001");
        eprintln!("     • Set REPRYNTT_SEEDS=10.0.0.19:5001");
        eprintln!("     • Add 'addnode=10.0.0.19:5001' to <data_dir>/node.conf");
        eprintln!();
    } else {
        println!(
            "   Seeds:    {}",
            args.seeds
                .iter()
                .map(|s| s.to_string())
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
    if let Some(ref py) = args.migrate_from {
        println!("   Migrate:  {}", py.display());
    }
    println!();

    // Boot the node
    let handle = match node::boot(args).await {
        Ok(h) => h,
        Err(e) => {
            eprintln!("❌ Boot failed: {}", e);
            std::process::exit(1);
        }
    };

    let health = node::health_check(&handle).await;
    println!();
    println!("🟢 Node online — {}", health);
    println!();

    // Wait for Ctrl-C
    match tokio::signal::ctrl_c().await {
        Ok(()) => {
            println!();
            println!("🛑 Shutting down…");
            handle.shutdown();

            // Final save
            {
                let p = handle.producer.lock().unwrap();
                if let Err(e) = p.save(&handle.storage) {
                    eprintln!("⚠️  Final save failed: {}", e);
                }
            }

            println!("✅ Shutdown complete");
        }
        Err(e) => {
            eprintln!("Signal error: {}", e);
        }
    }
}
