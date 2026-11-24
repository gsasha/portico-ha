//! TLS for the Protect console: the `crypto/tls` use of `api.go` and `ws.go`.
//!
//! Go builds a `tls.Config{ServerName: host, InsecureSkipVerify: !verify}` and, with
//! verification on, trusts the system CA bundle. This module does the same with rustls and the
//! aws-lc-rs provider that str0m already links (no OpenSSL, no `ring`): the CA bundle is read from
//! the places Go's `crypto/x509` reads on Linux (`SSL_CERT_FILE`, `SSL_CERT_DIR`, the usual
//! bundle files and directories), so no root-store crate is needed. With verification off
//! (`verify_ssl: false`, the installer's explicit opt-out for the console's self-signed
//! certificate) the chain and the name are not checked, but the handshake signature still is,
//! as in Go's `InsecureSkipVerify`.

use std::path::Path;
use std::sync::Arc;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{aws_lc_rs, CryptoProvider};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};
use tokio::sync::OnceCell;
use tokio_rustls::TlsConnector;

/// What `crypto/x509` tries on Linux, in its order.
const CERT_FILES: [&str; 6] = [
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/pki/tls/cacert.pem",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/cert.pem",
];
const CERT_DIRS: [&str; 2] = ["/etc/ssl/certs", "/etc/pki/tls/certs"];

/// `x509.SystemCertPool` as `loadSystemRoots` builds it on Linux: `SSL_CERT_FILE` replaces the
/// file list, `SSL_CERT_DIR` (colon separated) the directory list; the first readable file is
/// loaded, then every file of every directory. A certificate that does not parse is skipped.
pub fn system_roots() -> RootCertStore {
    system_roots_from(
        std::env::var("SSL_CERT_FILE").ok().as_deref(),
        std::env::var("SSL_CERT_DIR").ok().as_deref(),
    )
}

pub fn system_roots_from(file_env: Option<&str>, dir_env: Option<&str>) -> RootCertStore {
    let mut found: std::collections::BTreeSet<Vec<u8>> = std::collections::BTreeSet::new();
    let files: Vec<&str> = match file_env {
        Some(f) if !f.is_empty() => vec![f],
        _ => CERT_FILES.to_vec(),
    };
    for f in files {
        if let Ok(data) = std::fs::read(f) {
            add_pem(&mut found, &data);
            break;
        }
    }
    let dirs: Vec<&str> = match dir_env {
        Some(d) if !d.is_empty() => d.split(':').collect(),
        _ => CERT_DIRS.to_vec(),
    };
    for d in dirs {
        let Ok(rd) = std::fs::read_dir(Path::new(d)) else {
            continue;
        };
        let mut names: Vec<_> = rd.filter_map(|e| e.ok()).map(|e| e.path()).collect();
        names.sort();
        for p in names {
            if let Ok(data) = std::fs::read(&p) {
                add_pem(&mut found, &data);
            }
        }
    }
    // a certificate listed twice (a bundle and a directory) is one root, as in Go's pool
    let mut roots = RootCertStore::empty();
    roots.add_parsable_certificates(found.into_iter().map(CertificateDer::from));
    roots
}

fn add_pem(found: &mut std::collections::BTreeSet<Vec<u8>>, data: &[u8]) {
    for c in CertificateDer::pem_slice_iter(data).filter_map(|c| c.ok()) {
        found.insert(c.as_ref().to_vec());
    }
}

/// `InsecureSkipVerify`: any certificate for any name, but the signature of the handshake is
/// still checked against the certificate's key.
#[derive(Debug)]
struct NoVerify(Arc<CryptoProvider>);

impl ServerCertVerifier for NoVerify {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// The connector over the system pool, built once per process (Go caches
/// `x509.SystemCertPool` the same way): every `ProtectApi` of a door answer would otherwise read
/// the CA bundle and two directories again. An error is not kept, the next call tries again.
static SHARED_VERIFY: OnceCell<TlsConnector> = OnceCell::const_new();
static SHARED_NO_VERIFY: OnceCell<TlsConnector> = OnceCell::const_new();
#[cfg(feature = "test-seams")]
static SYSTEM_BUILDS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// How many times the verifying connector over the system pool was built in this process (tests).
#[cfg(feature = "test-seams")]
pub fn system_builds() -> usize {
    SYSTEM_BUILDS.load(std::sync::atomic::Ordering::SeqCst)
}

/// A TLS client configuration for the console, built on first use (reading the CA bundle is
/// file I/O, so it runs on the blocking pool) and shared by the API client and the websocket
/// (and, for the system pool, by every client of the process).
pub struct Tls {
    verify: bool,
    /// set only by [`Tls::with_roots`]: a pool of its own
    own: Option<TlsConnector>,
}

impl Tls {
    pub fn new(verify: bool) -> Self {
        Tls { verify, own: None }
    }

    /// Verifying against `roots` instead of the system pool (tests).
    #[cfg(feature = "test-seams")]
    pub fn with_roots(roots: RootCertStore) -> Self {
        Tls {
            verify: true,
            own: build(true, Some(roots)).ok(),
        }
    }

    /// Is the console's certificate checked (`verify_ssl`)?
    pub fn verifies(&self) -> bool {
        self.verify
    }

    /// Is this failed handshake a certificate the client does not TRUST (Go's
    /// `*tls.CertificateVerificationError`)? With verification off nothing is judged, so the
    /// same error (a certificate the library cannot read, a bad signature) is a failed
    /// handshake, not a reason to turn `verify_ssl` off.
    pub fn is_trust_failure(&self, e: &std::io::Error) -> bool {
        self.verify && is_cert_error(e)
    }

    pub async fn connector(&self) -> Result<TlsConnector, String> {
        if let Some(c) = &self.own {
            return Ok(c.clone());
        }
        let verify = self.verify;
        let cell = if verify {
            &SHARED_VERIFY
        } else {
            &SHARED_NO_VERIFY
        };
        cell.get_or_try_init(|| async move {
            tokio::task::spawn_blocking(move || {
                #[cfg(feature = "test-seams")]
                if verify {
                    SYSTEM_BUILDS.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                }
                build(verify, None)
            })
            .await
            .map_err(|e| format!("building the TLS configuration: {e}"))?
        })
        .await
        .cloned()
    }
}

/// The connector for `verify`; `roots` overrides the system pool (tests).
pub fn build(verify: bool, roots: Option<RootCertStore>) -> Result<TlsConnector, String> {
    let provider = Arc::new(aws_lc_rs::default_provider());
    let builder = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(|e| format!("building the TLS configuration: {e}"))?;
    let cfg = if verify {
        builder
            .with_root_certificates(roots.unwrap_or_else(system_roots))
            .with_no_client_auth()
    } else {
        builder
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(NoVerify(provider)))
            .with_no_client_auth()
    };
    Ok(TlsConnector::from(Arc::new(cfg)))
}

/// The name to put in the handshake: an IP literal stays one (no SNI), anything else must be a
/// valid DNS name.
pub fn server_name(hostname: &str) -> Result<ServerName<'static>, String> {
    ServerName::try_from(hostname.to_string())
        .map_err(|_| format!("invalid server name {hostname:?}"))
}

/// Is this handshake failure a certificate the client does not trust (Go's
/// `*tls.CertificateVerificationError`)?
pub fn is_cert_error(e: &std::io::Error) -> bool {
    e.get_ref()
        .and_then(|i| i.downcast_ref::<rustls::Error>())
        .is_some_and(|re| matches!(re, rustls::Error::InvalidCertificate(_)))
}

/// The `x509:` wording of a certificate failure on the REST path (the websocket path keeps Go's
/// one reason, `console certificate not trusted (verify_ssl)`).
pub fn cert_error_text(e: &std::io::Error) -> &'static str {
    use rustls::CertificateError as C;
    match e.get_ref().and_then(|i| i.downcast_ref::<rustls::Error>()) {
        Some(rustls::Error::InvalidCertificate(c)) => match c {
            C::UnknownIssuer => "x509: certificate signed by unknown authority",
            C::NotValidForName | C::NotValidForNameContext { .. } => {
                "x509: certificate is not valid for the requested name"
            }
            C::Expired
            | C::ExpiredContext { .. }
            | C::NotValidYet
            | C::NotValidYetContext { .. } => "x509: certificate has expired or is not yet valid",
            _ => "x509: certificate is not trusted",
        },
        _ => "x509: certificate is not trusted",
    }
}
