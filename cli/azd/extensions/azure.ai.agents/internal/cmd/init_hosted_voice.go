// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

package cmd

import (
	"azureaiagent/internal/exterrors"

	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"gopkg.in/yaml.v3"
)

// hostedVoiceSourceDescriptor is source capability metadata used by the Voice
// Bridge samples. It is not an azd agent definition and must not be adopted or
// overwritten by init.
type hostedVoiceSourceDescriptor struct {
	Name                  string   `yaml:"name"`
	Protocols             []string `yaml:"protocols"`
	VoiceLiveCompatible   string   `yaml:"voiceLiveCompatible"`
	BridgeProtocolVersion string   `yaml:"bridgeProtocolVersion"`
}

func loadHostedVoiceSourceDescriptor(path string) (*hostedVoiceSourceDescriptor, bool, error) {
	if strings.HasPrefix(path, "http://") || strings.HasPrefix(path, "https://") {
		return nil, false, nil
	}
	content, err := os.ReadFile(path) //nolint:gosec // path was discovered under the selected source directory
	if err != nil {
		if os.IsNotExist(err) {
			return nil, false, nil
		}
		return nil, false, err
	}
	var raw map[string]any
	if err := yaml.Unmarshal(content, &raw); err != nil {
		return nil, false, nil
	}
	if _, isAzdManifest := raw["template"]; isAzdManifest {
		return nil, false, nil
	}
	var descriptor hostedVoiceSourceDescriptor
	if err := yaml.Unmarshal(content, &descriptor); err != nil {
		return nil, false, nil
	}
	protocols := make([]string, 0, len(descriptor.Protocols))
	for _, protocol := range descriptor.Protocols {
		protocols = append(protocols, strings.ToLower(strings.TrimSpace(protocol)))
	}
	compatible := strings.TrimSpace(descriptor.Name) != "" &&
		slices.Contains(protocols, "invocations_ws") &&
		strings.EqualFold(strings.TrimSpace(descriptor.VoiceLiveCompatible), "true") &&
		strings.TrimSpace(descriptor.BridgeProtocolVersion) == "1.0"
	return &descriptor, compatible, nil
}

func isHostedVoiceSourceDescriptor(path string) bool {
	_, compatible, err := loadHostedVoiceSourceDescriptor(path)
	return err == nil && compatible
}

func applyHostedVoiceSourceDescriptor(flags *initFlags, path string, descriptor *hostedVoiceSourceDescriptor) error {
	if flags == nil || descriptor == nil {
		return fmt.Errorf("hosted voice source descriptor is required")
	}
	name, err := validateInitAgentName(descriptor.Name)
	if err != nil {
		return err
	}
	sourceDir, err := filepath.Abs(filepath.Dir(path))
	if err != nil {
		return fmt.Errorf("resolving hosted voice sample directory: %w", err)
	}
	flags.kind = kindFlagHostedVoice
	if strings.TrimSpace(flags.agentName) == "" {
		flags.agentName = name
	}
	flags.src = sourceDir
	flags.deployMode = "code"
	flags.protocols = []string{"invocations_ws"}
	flags.depResolution = "bundled"
	if isDotnetProject(sourceDir) {
		flags.runtime = "dotnet_10"
	} else if isPythonProject(sourceDir) {
		flags.runtime = "python_3_13"
	}
	if flags.runtime == "" {
		return exterrors.Validation(
			exterrors.CodeInvalidParameter,
			"could not detect a supported runtime for the hosted voice sample",
			"place agent.manifest.yaml next to a .NET or Python agent project",
		)
	}
	flags.entryPoint = detectDefaultEntryPoint(sourceDir, flags.runtime)
	validEntryPoint := strings.TrimSpace(flags.entryPoint) != ""
	if flags.runtime != "dotnet_10" {
		validEntryPoint = validEntryPoint && fileExists(filepath.Join(sourceDir, flags.entryPoint))
	}
	if !validEntryPoint {
		return exterrors.Validation(
			exterrors.CodeInvalidParameter,
			"could not detect the hosted voice sample entry point",
			"ensure the sample contains its expected .NET assembly or Python entry module",
		)
	}
	return nil
}
