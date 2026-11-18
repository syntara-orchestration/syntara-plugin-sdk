# Go steps

`normalize-records` trims surrounding whitespace from every record name,
lowercases it, and returns the normalized list with its count. It is
implemented in Go; its workload image serves it through the container ABI.
