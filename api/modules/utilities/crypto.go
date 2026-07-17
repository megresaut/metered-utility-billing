package utilities

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
)

// Credential storage: AES-256-GCM with a single application-level master key
// (CRED_MASTER_KEY env), random nonce per row. The plaintext is a small JSON
// object ({"password": "...", "sec_answer": "..."}) so providers that need
// more than a password (e.g. fios security answer) fit the same scheme.
// Decrypt happens only at scrape-dispatch time; secret values are never
// logged. This deliberately replaces ra-avm's base64/env-var scheme, which
// was not real encryption.

var errNoMasterKey = errors.New("CRED_MASTER_KEY not configured")

func (m *Module) encryptSecrets(secrets map[string]string) (ciphertext, nonce []byte, err error) {
	if len(m.cfg.CredMasterKey) != 32 {
		return nil, nil, errNoMasterKey
	}
	plain, err := json.Marshal(secrets)
	if err != nil {
		return nil, nil, err
	}
	block, err := aes.NewCipher(m.cfg.CredMasterKey)
	if err != nil {
		return nil, nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, nil, err
	}
	nonce = make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, nil, err
	}
	return gcm.Seal(nil, nonce, plain, nil), nonce, nil
}

func (m *Module) decryptSecrets(ciphertext, nonce []byte) (map[string]string, error) {
	if len(ciphertext) == 0 {
		return map[string]string{}, nil
	}
	if len(m.cfg.CredMasterKey) != 32 {
		return nil, errNoMasterKey
	}
	block, err := aes.NewCipher(m.cfg.CredMasterKey)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	plain, err := gcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, fmt.Errorf("credential decrypt failed (wrong CRED_MASTER_KEY?): %w", err)
	}
	out := map[string]string{}
	if err := json.Unmarshal(plain, &out); err != nil {
		return nil, err
	}
	return out, nil
}
