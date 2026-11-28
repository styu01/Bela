// VAULTMAINONLY (KISPROGI 6431 plan, BELA 6432 decisions): the vault entries whose VALUE only the main agent or the
// signed-in owner may get: credentials that act outward (send mail as the owner, upload to the web host, run Claude on
// the owner's account). The list is fixed here; .env can only ADD to it (VAULT_PROTECTED_IDS, comma-separated), never
// remove an entry, and the store cannot change it.
//
// VAULT_MAIN_ONLY=audit in .env is the emergency switch back (BELA 6432 (ii)): every refusal is then only logged and
// the call goes through. Anything else (unset included) enforces. Both keys are read from .env on every check, so the
// switch needs no restart. .env is not a Settings key (store/config-overrides.json cannot set it), but the same OS
// user can edit the file: the same limit as reading the vault from disk (vault-guard.ts header).
import { readEnvFile } from '../env.js'
import { logger } from '../logger.js'

export const BUILTIN_PROTECTED_VAULT_IDS: readonly string[] = [
  'gmail_kiss_i_god_app_pw',     // the Gmail app password (kiss.i.god), send-gmail's key
  'info@woowebsite.eu jelszó',   // the woowebsite.eu mailbox, send_outreach's key
  'MARVEEN_IO_API_KULCS',        // the marveen.io API key: on the list before it is in the vault
  'Moho claude token',           // BELA 6432 (i)
  'woowebsite ftp jelszó',       // BELA 6432 (i)
  'vault-vedett-kanari',         // a dummy canary for the live smoke test
]

export type VaultMainOnlyMode = 'enforce' | 'audit'

export function vaultMainOnlyMode(read: (keys: string[]) => Record<string, string> = readEnvFile): VaultMainOnlyMode {
  return (read(['VAULT_MAIN_ONLY'])['VAULT_MAIN_ONLY'] ?? '').trim().toLowerCase() === 'audit' ? 'audit' : 'enforce'
}

export function protectedVaultIds(read: (keys: string[]) => Record<string, string> = readEnvFile): Set<string> {
  const extra = (read(['VAULT_PROTECTED_IDS'])['VAULT_PROTECTED_IDS'] ?? '').split(',').map((s) => s.trim()).filter(Boolean)
  return new Set([...BUILTIN_PROTECTED_VAULT_IDS, ...extra].map(normId))
}

export function isProtectedVaultId(id: unknown, read: (keys: string[]) => Record<string, string> = readEnvFile): boolean {
  return typeof id === 'string' && protectedVaultIds(read).has(normId(id))
}

/** An id as compared here: trimmed and NFC (an accented id typed or stored in decomposed form is the same id). */
function normId(id: string): string {
  return id.trim().normalize('NFC')
}

/** A guard for code that has no request (a binding sync, a provider launch): true when the id may be used there.
 *  Protected ids are refused (logged) unless the mode is audit. */
export function protectedVaultIdUsable(id: unknown, where: string, read: (keys: string[]) => Record<string, string> = readEnvFile): boolean {
  if (!isProtectedVaultId(id, read)) return true
  const mode = vaultMainOnlyMode(read)
  logger.warn({ vaultId: String(id), where, mode }, mode === 'audit' ? 'vault: protected entry used, WOULD be refused (audit mode)' : 'vault: protected entry refused')
  return mode === 'audit'
}
