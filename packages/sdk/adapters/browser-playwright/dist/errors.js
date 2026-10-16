export class ProviderError extends Error {
    code;
    details;
    constructor(code, message = code, details) {
        super(message);
        this.code = code;
        this.details = details;
    }
}
export function requireValue(value, message) {
    if (!value)
        throw new ProviderError("INVALID_ARGUMENT", message);
}
