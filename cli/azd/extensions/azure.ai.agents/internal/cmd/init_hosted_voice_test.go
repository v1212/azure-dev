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
