//! Cryptographic primitives for repryntt.
//!
//! Ed25519 signing and verification, matching Python's
//! `cryptography.hazmat.primitives.asymmetric.ed25519` exactly.

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand::rngs::OsRng;
use sha3::{Digest, Sha3_256};

/// Generate an Ed25519 keypair.
///
/// Returns `(private_key_32_bytes, public_key_32_bytes)`.
pub fn generate_keypair() -> (Vec<u8>, Vec<u8>) {
    let mut csprng = OsRng;
    let signing_key = SigningKey::generate(&mut csprng);
    let verifying_key = signing_key.verifying_key();
    (
        signing_key.to_bytes().to_vec(),
        verifying_key.to_bytes().to_vec(),
    )
}

/// Sign `data` with the given 32-byte Ed25519 private key.
///
/// Returns a 64-byte signature.
pub fn sign(data: &[u8], private_key: &[u8]) -> Vec<u8> {
    let sk_bytes: [u8; 32] = private_key
        .try_into()
        .expect("private key must be 32 bytes");
    let signing_key = SigningKey::from_bytes(&sk_bytes);
    let sig = signing_key.sign(data);
    sig.to_bytes().to_vec()
}

/// Verify an Ed25519 signature.
pub fn verify(data: &[u8], signature: &[u8], public_key: &[u8]) -> bool {
    let Ok(pk_bytes) = <[u8; 32]>::try_from(public_key) else {
        return false;
    };
    let Ok(sig_bytes) = <[u8; 64]>::try_from(signature) else {
        return false;
    };
    let Ok(verifying_key) = VerifyingKey::from_bytes(&pk_bytes) else {
        return false;
    };
    let sig = Signature::from_bytes(&sig_bytes);
    verifying_key.verify(data, &sig).is_ok()
}

/// Derive a repryntt address from a public key: `sha3_256(pubkey)[:40]` (hex).
pub fn address_from_pubkey(public_key: &[u8]) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(public_key);
    let full = hex::encode(hasher.finalize());
    full[..40].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sign_verify_roundtrip() {
        let (sk, pk) = generate_keypair();
        let msg = b"hello repryntt";
        let sig = sign(msg, &sk);
        assert!(verify(msg, &sig, &pk));
        // Tampered message should fail
        assert!(!verify(b"tampered", &sig, &pk));
    }

    #[test]
    fn test_address_derivation() {
        let (_, pk) = generate_keypair();
        let addr = address_from_pubkey(&pk);
        assert_eq!(addr.len(), 40);
    }
}
