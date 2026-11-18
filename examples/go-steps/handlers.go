package main

import (
	"errors"
	"fmt"
	"strings"
)

// errInvalidInput marks an input the handler cannot process.
var errInvalidInput = errors.New("invalid input")

// handlers maps each manifest entryPoint to its implementation. The
// entryPoint string is opaque to Syntara: whatever serves this plugin's
// workload image dispatches by step revision ID and resolves that value by
// this table.
var handlers = map[string]func(inputs map[string]any) (any, error){
	"handlers.NormalizeRecords": NormalizeRecords,
}

type normalizeRecordsOutput struct {
	Count      int      `json:"count"`
	Normalized []string `json:"normalized"`
}

// NormalizeRecords trims and lowercases every record name. The input contract
// in steps/normalize-records/manifest.yaml declares "records" as its only
// property with additionalProperties: false, so any other field is invalid
// input, exactly like a wrong-typed records value.
func NormalizeRecords(inputs map[string]any) (any, error) {
	for field := range inputs {
		if field != "records" {
			return nil, fmt.Errorf("%w: unexpected field %q", errInvalidInput, field)
		}
	}
	records, ok := inputs["records"].([]any)
	if !ok {
		return nil, errInvalidInput
	}
	normalized := make([]string, 0, len(records))
	for _, record := range records {
		name, ok := record.(string)
		if !ok {
			return nil, errInvalidInput
		}
		normalized = append(normalized, strings.ToLower(strings.TrimSpace(name)))
	}
	return normalizeRecordsOutput{Count: len(normalized), Normalized: normalized}, nil
}
