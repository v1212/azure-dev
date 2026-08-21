// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

package cmd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestIsHostedVoiceSourceDescriptor(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "agent.manifest.yaml")
	require.NoError(t, os.WriteFile(path, []byte(`
name: voice-hosted-agent-dotnet
protocols:
  - invocations_ws
voiceLiveCompatible: "true"
bridgeProtocolVersion: "1.0"
`), 0600))
	require.True(t, isHostedVoiceSourceDescriptor(path))
}

func TestIsHostedVoiceSourceDescriptorRejectsIncompatibleDescriptor(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "agent.manifest.yaml")
	require.NoError(t, os.WriteFile(path, []byte(`
protocols:
  - responses
voiceLiveCompatible: "true"
bridgeProtocolVersion: "2.0"
`), 0600))
	require.False(t, isHostedVoiceSourceDescriptor(path))
}

func TestApplyHostedVoiceSourceDescriptorDotnet(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	manifestPath := filepath.Join(dir, "agent.manifest.yaml")
	require.NoError(t, os.WriteFile(manifestPath, []byte("name: voice-sample\n"), 0600))
	require.NoError(t, os.WriteFile(filepath.Join(dir, "VoiceHostedAgent.csproj"), []byte(`
<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><AssemblyName>VoiceHostedAgent</AssemblyName></PropertyGroup></Project>
`), 0600))
	flags := &initFlags{}
	err := applyHostedVoiceSourceDescriptor(flags, manifestPath, &hostedVoiceSourceDescriptor{Name: "voice-sample"})
	require.NoError(t, err)
	require.Equal(t, kindFlagHostedVoice, flags.kind)
	require.Equal(t, "voice-sample", flags.agentName)
	require.Equal(t, dir, flags.src)
	require.Equal(t, "code", flags.deployMode)
	require.Equal(t, "dotnet_10", flags.runtime)
	require.Equal(t, "VoiceHostedAgent.dll", flags.entryPoint)
	require.Equal(t, "bundled", flags.depResolution)
	require.Equal(t, []string{"invocations_ws"}, flags.protocols)
}

func TestApplyHostedVoiceSourceDescriptorPreservesExplicitName(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	manifestPath := filepath.Join(dir, "agent.manifest.yaml")
	require.NoError(t, os.WriteFile(manifestPath, []byte("name: descriptor-name\n"), 0600))
	require.NoError(t, os.WriteFile(filepath.Join(dir, "VoiceHostedAgent.csproj"), []byte(`
<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><AssemblyName>VoiceHostedAgent</AssemblyName></PropertyGroup></Project>
`), 0600))
	flags := &initFlags{agentName: "explicit-name"}
	require.NoError(t, applyHostedVoiceSourceDescriptor(
		flags, manifestPath, &hostedVoiceSourceDescriptor{Name: "descriptor-name"},
	))
	require.Equal(t, "explicit-name", flags.agentName)
}

func TestApplyHostedVoiceSourceDescriptorRejectsMissingEntrypoint(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	manifestPath := filepath.Join(dir, "agent.manifest.yaml")
	require.NoError(t, os.WriteFile(manifestPath, []byte("name: voice-sample\n"), 0600))
	require.NoError(t, os.WriteFile(filepath.Join(dir, "requirements.txt"), []byte("websockets\n"), 0600))
	err := applyHostedVoiceSourceDescriptor(
		&initFlags{}, manifestPath, &hostedVoiceSourceDescriptor{Name: "voice-sample"},
	)
	require.ErrorContains(t, err, "could not detect the hosted voice sample entry point")
}

func TestHostedVoiceDescriptorDoesNotMatchAzdManifest(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "agent.yaml")
	require.NoError(t, os.WriteFile(path, []byte(`
name: manifest
protocols: [invocations_ws]
voiceLiveCompatible: "true"
bridgeProtocolVersion: "1.0"
template:
  kind: hosted
  name: agent
`), 0600))
	_, compatible, err := loadHostedVoiceSourceDescriptor(path)
	require.NoError(t, err)
	require.False(t, compatible)
}
