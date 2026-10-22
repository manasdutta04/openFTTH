//! AI keys in the OS credential store (Windows Credential Manager, encrypted for the signed-in
//! user). The harness only ever holds them in memory: at start the app hands over every stored
//! key, and moves any key an older version left in `engines.json` into the store.

use openftth_client::Client;

const SERVICE: &str = "openFTTH";
/// Engines that take a key.
const KINDS: [&str; 2] = ["openai", "openrouter"];

#[cfg(windows)]
fn entry(kind: &str) -> keyring::Result<keyring::Entry> {
    keyring::Entry::new(SERVICE, &format!("engine:{kind}"))
}

/// Save (or, with an empty key, remove) an engine's key.
pub fn store(kind: &str, key: &str) -> Result<(), String> {
    #[cfg(windows)]
    {
        let e = entry(kind).map_err(|e| e.to_string())?;
        if key.trim().is_empty() {
            match e.delete_credential() {
                Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
                Err(err) => Err(err.to_string()),
            }
        } else {
            e.set_password(key.trim()).map_err(|e| e.to_string())
        }
    }
    #[cfg(not(windows))]
    {
        let _ = (kind, key);
        Err("no credential store on this platform yet".into())
    }
}

fn load(kind: &str) -> Option<String> {
    #[cfg(windows)]
    {
        entry(kind).ok()?.get_password().ok()
    }
    #[cfg(not(windows))]
    {
        let _ = kind;
        None
    }
}

/// At start: move keys an older version saved in a file into the store, then hand every stored
/// key to the harness (memory only).
pub fn restore(client: &Client) {
    if let Ok(old) = client.take_legacy_keys() {
        for (kind, key) in old.as_object().into_iter().flatten() {
            if let Some(key) = key.as_str() {
                let _ = store(kind, key);
            }
        }
    }
    for kind in KINDS {
        if let Some(key) = load(kind) {
            let _ = client.set_engine_key(kind, &key);
        }
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::{load, store};

    #[test]
    fn a_key_round_trips_through_the_credential_store_and_can_be_removed() {
        let kind = "selftest";
        store(kind, "sk-roundtrip").unwrap();
        assert_eq!(load(kind).as_deref(), Some("sk-roundtrip"));
        store(kind, "").unwrap();
        assert_eq!(load(kind), None);
    }
}
