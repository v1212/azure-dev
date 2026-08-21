// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

package cmd

import (
	"os"
	"slices"
	"strings"

	"gopkg.in/yaml.v3"
)

// hostedVoiceSourceDescriptor is source capability metadata used by the Voice
// Bridge samples. It is not an azd agent definition and must not be adopted or
// overwritten by init.
type hostedVoiceSourceDescriptor struct {
	Protocols             []string `yaml:"protocols"`
	VoiceLiveCompatible   string   `yaml:"voiceLiveCompatible"`
	BridgeProtocolVersion string   `yaml:"bridgeProtocolVersion"`
}

func isHostedVoiceSourceDescriptor(path string) bool {
	content, err := os.ReadFile(path) //nolint:gosec // path was discovered under the selected source directory
	if err != nil {
		return false
	}
	var descriptor hostedVoiceSourceDescriptor
	if yaml.Unmarshal(content, &descriptor) != nil {
		return false
	}
	protocols := make([]string, 0, len(descriptor.Protocols))
	for _, protocol := range descriptor.Protocols {
		protocols = append(protocols, strings.ToLower(strings.TrimSpace(protocol)))
	}
	return slices.Contains(protocols, "invocations_ws") &&
		strings.EqualFold(strings.TrimSpace(descriptor.VoiceLiveCompatible), "true") &&
		strings.TrimSpace(descriptor.BridgeProtocolVersion) == "1.0"
}
