# Homebrew formula for the netdash macOS collector.
#
# PLACEHOLDERS -- fill these in before publishing to the tap:
#   * homepage / url    -> your GitHub repo and release tarball
#   * sha256            -> shasum -a 256 of that tarball
#   * the tap itself    -> e.g. homebrew-tap repo, installed as <user>/tap/netdash-collector
#
# Publish flow once the repo exists:
#   git tag v0.1.0 && git push --tags
#   curl -sL https://github.com/<user>/<repo>/archive/refs/tags/v0.1.0.tar.gz | shasum -a 256
#   ...paste that sha256 below, commit this file into the tap repo as Formula/netdash-collector.rb
class NetdashCollector < Formula
  desc "Pushes CPU, memory and disk metrics to a netdash server"
  homepage "https://github.com/USER/REPO"
  url "https://github.com/USER/REPO/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "MIT"

  def install
    bin.install "collectors/macos/netdash-collector.sh" => "netdash-collector"
    # Installed only if absent, so `brew upgrade` never clobbers a real token.
    (etc/"netdash").install "homebrew/collector.conf.sample" => "collector.conf"
  end

  service do
    run [opt_bin/"netdash-collector"]
    run_type :interval
    interval 60
    log_path var/"log/netdash-collector.log"
    error_log_path var/"log/netdash-collector.log"
  end

  def caveats
    <<~EOS
      Set the server URL and token before starting:
        $EDITOR #{etc}/netdash/collector.conf

      Then start the launchd timer (Homebrew never auto-starts services on install):
        brew services start netdash-collector

      Verify what it will send:
        netdash-collector --print

      Updates later:
        brew update && brew upgrade netdash-collector
      (brew services restarts the job automatically on upgrade.)
    EOS
  end

  test do
    assert_match "cpu_pct", shell_output("#{bin}/netdash-collector --print")
  end
end
