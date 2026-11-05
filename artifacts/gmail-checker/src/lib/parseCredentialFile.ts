import * as XLSX from "xlsx";

export type ParsedCredential = {
  email: string;
  password: string;
  totp?: string;
};

const EMAIL_ALIASES = ["email", "username", "user", "mail", "e-mail", "gmail"];
const PASS_ALIASES = ["password", "pass", "pwd"];
const TOTP_ALIASES = [
  "2fa",
  "2fa secret",
  "2fasecret",
  "totp",
  "totp secret",
  "totpsecret",
  "two factor",
  "twofactor",
  "two factor secret",
];

function normalizeHeader(value: string): string {
  return value.toLowerCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
}

function cellText(value: unknown): string {
  if (value == null) return "";
  return String(value).trim();
}

function headerMatches(cell: string, aliases: string[]): boolean {
  const normalized = normalizeHeader(cell);
  const compact = normalized.replace(/\s+/g, "");
  return aliases.some(alias => {
    const aliasCompact = alias.replace(/\s+/g, "");
    return normalized === alias || compact === aliasCompact;
  });
}

function looksLikeHeaderRow(cells: string[]): boolean {
  // Data rows often contain @ in the email column — never treat those as headers.
  if (cells.some(cell => cell.includes("@"))) return false;
  return cells.some(cell =>
    headerMatches(cell, EMAIL_ALIASES)
    || headerMatches(cell, PASS_ALIASES)
    || headerMatches(cell, TOTP_ALIASES),
  );
}

function objectKeysLookLikeHeaders(keys: string[]): boolean {
  if (keys.length === 0) return false;
  if (keys.some(key => key.includes("@"))) return false;
  return keys.some(key =>
    headerMatches(key, EMAIL_ALIASES)
    || headerMatches(key, PASS_ALIASES)
    || headerMatches(key, TOTP_ALIASES),
  );
}

function pickColumnIndex(headers: string[], aliases: string[]): number {
  for (let i = 0; i < headers.length; i++) {
    if (headerMatches(headers[i] ?? "", aliases)) return i;
  }
  return -1;
}

function buildCredential(email: string, password: string, totp?: string): ParsedCredential | null {
  const cleanEmail = email.trim();
  const cleanPassword = password.trim();
  const cleanTotp = totp?.replace(/\s+/g, "").trim();
  if (!cleanEmail || !cleanPassword) return null;
  if (headerMatches(cleanEmail, EMAIL_ALIASES)) return null;
  return {
    email: cleanEmail,
    password: cleanPassword,
    ...(cleanTotp ? { totp: cleanTotp } : {}),
  };
}

function parseRows(rows: unknown[][]): ParsedCredential[] {
  const nonEmptyRows = rows
    .map(row => (Array.isArray(row) ? row : []).map(cellText))
    .filter(row => row.some(Boolean));
  if (nonEmptyRows.length === 0) return [];

  const firstRow = nonEmptyRows[0] ?? [];
  const hasHeader = looksLikeHeaderRow(firstRow);
  const dataRows = hasHeader ? nonEmptyRows.slice(1) : nonEmptyRows;
  const headers = hasHeader ? firstRow : ["Email", "Password", "2FA Secret"];

  const emailIdx = pickColumnIndex(headers, EMAIL_ALIASES);
  const passIdx = pickColumnIndex(headers, PASS_ALIASES);
  const totpIdx = pickColumnIndex(headers, TOTP_ALIASES);

  const resolvedEmailIdx = emailIdx >= 0 ? emailIdx : 0;
  const resolvedPassIdx = passIdx >= 0 ? passIdx : 1;
  const resolvedTotpIdx = totpIdx >= 0 ? totpIdx : 2;

  const credentials: ParsedCredential[] = [];
  for (const row of dataRows) {
    const credential = buildCredential(
      row[resolvedEmailIdx] ?? "",
      row[resolvedPassIdx] ?? "",
      row[resolvedTotpIdx] ?? "",
    );
    if (credential) credentials.push(credential);
  }
  return credentials;
}

function parseObjectRows(objectRows: Record<string, unknown>[]): ParsedCredential[] {
  const credentials: ParsedCredential[] = [];
  for (const row of objectRows) {
    const normalized = Object.fromEntries(
      Object.entries(row).map(([key, value]) => [normalizeHeader(key), cellText(value)]),
    );
    const email = EMAIL_ALIASES.map(alias => normalized[alias]).find(Boolean)
      ?? Object.entries(normalized).find(([key]) => headerMatches(key, EMAIL_ALIASES))?.[1]
      ?? "";
    const password = PASS_ALIASES.map(alias => normalized[alias]).find(Boolean)
      ?? Object.entries(normalized).find(([key]) => headerMatches(key, PASS_ALIASES))?.[1]
      ?? "";
    const totp = TOTP_ALIASES.map(alias => normalized[alias]).find(Boolean)
      ?? Object.entries(normalized).find(([key]) => headerMatches(key, TOTP_ALIASES))?.[1]
      ?? "";
    const credential = buildCredential(email, password, totp);
    if (credential) credentials.push(credential);
  }
  return credentials;
}

export function parseCredentialsFromWorkbook(workbook: XLSX.WorkBook): ParsedCredential[] {
  const sheetName = workbook.SheetNames[0];
  if (!sheetName) return [];
  const sheet = workbook.Sheets[sheetName];
  if (!sheet) return [];

  // Prefer raw rows — works for headerless sheets (col A=email, B=pass, C=2fa).
  const matrixRows = XLSX.utils.sheet_to_json<unknown[]>(sheet, { header: 1, defval: "" });
  const fromMatrix = parseRows(matrixRows);
  if (fromMatrix.length > 0) return fromMatrix;

  const objectRows = XLSX.utils.sheet_to_json<Record<string, unknown>>(sheet, { defval: "" });
  if (objectRows.length > 0 && objectKeysLookLikeHeaders(Object.keys(objectRows[0] ?? {}))) {
    const fromObjects = parseObjectRows(objectRows);
    if (fromObjects.length > 0) return fromObjects;
  }

  return [];
}

export async function parseCredentialsFromFile(file: File): Promise<ParsedCredential[]> {
  const buffer = await file.arrayBuffer();
  const workbook = XLSX.read(buffer, { type: "array" });
  return parseCredentialsFromWorkbook(workbook);
}

export function credentialsToInputText(credentials: ParsedCredential[]): string {
  return credentials
    .map(c => [c.email, c.password, c.totp].filter(Boolean).join(":"))
    .join("\n");
}

export type SmtpEntry = {
  series: number;
  email: string;
  password?: string;
  totp?: string;
};

/** Strip leading list markers such as `1.`, `2)`, or `3:` from a pasted line. */
export function stripSeriesPrefix(line: string): string {
  return line.trim().replace(/^\d+[\.\)\:\-\s]+/, "").trim();
}

function parseCredentialLine(line: string): { email: string; password?: string; totp?: string } | null {
  const cleaned = stripSeriesPrefix(line);
  if (!cleaned) return null;

  if (!cleaned.includes(":")) {
    const email = cleaned.trim();
    if (!email.includes("@")) return null;
    return { email };
  }

  const parts = cleaned.split(":");
  if (parts.length < 2) return null;
  const email = parts[0]?.trim() ?? "";
  if (!email) return null;

  if (parts.length === 2) {
    const password = parts[1]?.trim() ?? "";
    return password ? { email, password } : { email };
  }

  const password = parts.slice(1, -1).join(":").trim();
  const totp = parts[parts.length - 1]?.replace(/\s+/g, "").trim();
  return {
    email,
    password,
    ...(totp ? { totp } : {}),
  };
}

/** Parse SMTP checker input — supports plain emails or email:pass:2fa rows. */
export function parseSmtpPayload(text: string): SmtpEntry[] {
  const lines = text.split("\n");
  const entries: SmtpEntry[] = [];

  lines.forEach((line, index) => {
    const parsed = parseCredentialLine(line);
    if (!parsed?.email) return;
    entries.push({
      series: index + 1,
      email: parsed.email,
      ...(parsed.password ? { password: parsed.password } : {}),
      ...(parsed.totp ? { totp: parsed.totp } : {}),
    });
  });

  return entries;
}

export function formatSmtpPayloadWithSeries(entries: SmtpEntry[]): string {
  return entries
    .map(entry => {
      const payload = [entry.email, entry.password, entry.totp].filter(Boolean).join(":");
      return `${entry.series}. ${payload}`;
    })
    .join("\n");
}
