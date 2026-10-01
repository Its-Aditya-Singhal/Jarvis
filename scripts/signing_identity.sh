#!/usr/bin/env bash
# A local code-signing identity for JARVIS builds, created once and reused for every rebuild.
#
#   source scripts/signing_identity.sh    # sets SIGN_ID and SIGN_KEYCHAIN (or leaves SIGN_ID="-")
#
# An ad hoc signature changes with every build, so macOS treats each rebuild as a new app: the
# Screen Recording / Accessibility / Microphone switches still show "on" for the old build but
# don't apply to the new one, and the Keychain item holding JARVIS's data key asks again. Signed
# with the same certificate every time, the app's identity stays put and so do those grants.
#
# The certificate is self-signed and only ever used on this Mac. It lives in its own keychain
# (~/Library/Keychains/jarvis-signing.keychain-db) whose random password is kept in
# ~/.config/jarvis/signing-keychain-pass (owner-only), so builds never need the login password.
# If anything here fails the build falls back to an ad hoc signature and says so; CI builds
# (the release workflow) are always ad hoc.

SIGN_NAME="JARVIS Local Signing"
SIGN_KEYCHAIN="$HOME/Library/Keychains/jarvis-signing.keychain-db"
_SIGN_PASS_FILE="$HOME/.config/jarvis/signing-keychain-pass"
SIGN_ID="-"

_jarvis_make_identity() {
  local tmp pass
  tmp="$(mktemp -d)"
  mkdir -p "$(dirname "$_SIGN_PASS_FILE")"
  ( umask 077; openssl rand -hex 24 > "$_SIGN_PASS_FILE" )
  pass="$(cat "$_SIGN_PASS_FILE")"
  cat > "$tmp/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $SIGN_NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$tmp/cert.cnf" \
    -keyout "$tmp/key.pem" -out "$tmp/cert.pem" >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
  # 3DES/SHA1 so both LibreSSL and OpenSSL 3 write a .p12 the macOS `security` tool can read
  openssl pkcs12 -export -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -name "$SIGN_NAME" \
    -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1 -passout "pass:$pass" -out "$tmp/id.p12" \
    >/dev/null 2>&1 || { rm -rf "$tmp"; return 1; }
  security delete-keychain "$SIGN_KEYCHAIN" >/dev/null 2>&1 || true
  security create-keychain -p "$pass" "$SIGN_KEYCHAIN" &&
    security set-keychain-settings "$SIGN_KEYCHAIN" &&
    security unlock-keychain -p "$pass" "$SIGN_KEYCHAIN" &&
    security import "$tmp/id.p12" -k "$SIGN_KEYCHAIN" -P "$pass" -T /usr/bin/codesign >/dev/null &&
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$pass" "$SIGN_KEYCHAIN" >/dev/null
  local rc=$?
  rm -rf "$tmp"
  return $rc
}

_jarvis_has_identity() {
  security find-certificate -c "$SIGN_NAME" "$SIGN_KEYCHAIN" >/dev/null 2>&1
}

if [[ "$(uname -s)" == "Darwin" && -z "${CI:-}" ]]; then  # release runners stay ad hoc
  if ! { [[ -f "$_SIGN_PASS_FILE" ]] && _jarvis_has_identity; }; then
    echo "==> Creating the local signing identity \"$SIGN_NAME\" (once)"
    _jarvis_make_identity || echo "warning: couldn't create a signing identity" >&2
  fi
  if [[ -f "$_SIGN_PASS_FILE" ]] && _jarvis_has_identity &&
     security unlock-keychain -p "$(cat "$_SIGN_PASS_FILE")" "$SIGN_KEYCHAIN" 2>/dev/null; then
    # codesign looks identities up in the search list; add ours without dropping the others
    if ! security list-keychains -d user | grep -q "jarvis-signing.keychain-db"; then
      eval "security list-keychains -d user -s \"$SIGN_KEYCHAIN\" $(security list-keychains -d user | tr '\n' ' ')"
    fi
    # by its hash, so a second certificate with the same name can never be picked instead
    SIGN_ID="$(security find-certificate -c "$SIGN_NAME" -Z "$SIGN_KEYCHAIN" | awk '/SHA-1 hash/ {print $NF; exit}')"
    [[ -n "$SIGN_ID" ]] || SIGN_ID="-"
  else
    echo "warning: signing ad hoc; macOS permissions and the Keychain prompt will reset after this rebuild" >&2
  fi
fi
