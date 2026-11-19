package main

import (
	"errors"
	"reflect"
	"testing"
)

func TestNormalizeRecordsTrimsAndLowercases(t *testing.T) {
	output, err := NormalizeRecords(map[string]any{"records": []any{"  Alpha ", "BETA", " École"}})
	if err != nil {
		t.Fatal(err)
	}
	want := normalizeRecordsOutput{Count: 3, Normalized: []string{"alpha", "beta", "école"}}
	if !reflect.DeepEqual(output, want) {
		t.Fatalf("got %#v, want %#v", output, want)
	}
}

func TestNormalizeRecordsRejectsTheWrongShape(t *testing.T) {
	for _, inputs := range []map[string]any{{}, {"records": "alpha"}, {"records": []any{1}}} {
		if _, err := NormalizeRecords(inputs); !errors.Is(err, errInvalidInput) {
			t.Fatalf("expected invalid input for %#v, got %v", inputs, err)
		}
	}
}

// The manifest's input contract declares additionalProperties: false, so an
// undeclared field must be rejected exactly like a wrong-typed records value.
func TestNormalizeRecordsRejectsUndeclaredFields(t *testing.T) {
	_, err := NormalizeRecords(map[string]any{"records": []any{}, "unexpected": true})
	if !errors.Is(err, errInvalidInput) {
		t.Fatalf("expected invalid input, got %v", err)
	}
}
