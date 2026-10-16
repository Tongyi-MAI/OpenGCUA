export class ProviderError extends Error {
  constructor(
    public code: string,
    message = code,
    public details?: unknown,
  ) {
    super(message);
  }
}
export function requireValue(value: unknown, message: string): asserts value {
  if (!value) throw new ProviderError("INVALID_ARGUMENT", message);
}
