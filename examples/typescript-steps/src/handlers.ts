// The step types this plugin's workload image serves, keyed by manifest
// entryPoint. The entryPoint string is opaque to Syntara: whatever serves the
// image dispatches by step revision ID and resolves that value by this table.

export type Inputs = Record<string, unknown>;

export class InvalidInputError extends Error {}

export interface NormalizeRecordsOutput {
  count: number;
  normalized: string[];
}

/**
 * Trim and lowercase every record name. The input contract in
 * steps/normalize-records/manifest.yaml declares "records" as its only
 * property with additionalProperties: false, so any other field is invalid
 * input, exactly like a wrong-typed records value.
 */
export function normalizeRecords(inputs: Inputs): NormalizeRecordsOutput {
  for (const field of Object.keys(inputs)) {
    if (field !== 'records') {
      throw new InvalidInputError(`unexpected field ${JSON.stringify(field)}`);
    }
  }
  const { records } = inputs;
  if (!Array.isArray(records) || !records.every((record) => typeof record === 'string')) {
    throw new InvalidInputError('records must be an array of strings');
  }
  const normalized = records.map((record: string) => record.trim().toLowerCase());
  return { count: normalized.length, normalized };
}

export const handlers: Readonly<Record<string, (inputs: Inputs) => unknown>> = {
  'handlers.ts#normalizeRecords': normalizeRecords,
};
