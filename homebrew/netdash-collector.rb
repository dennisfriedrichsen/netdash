# Homebrew formula for the netdash macOS collector.
#
# Tap:  https://github.com/dennisfriedrichsen/homebrew-tap
# Ships as: brew install dennisfriedrichsen/tap/netdash-collector
#
# Re-publishing a new version:
#   git tag 0.1.5 && git push origin 0.1.5
#   curl -sL https://github.com/dennisfriedrichsen/netdash/archive/refs/tags/0.1.5.tar.gz | shasum -a 256
class NetdashCollector < Formula
  desc "Pushes CPU, memory and disk metrics to a netdash server"
  homepage "https://github.com/dennisfriedrichsen/netdash"
  url "https://github.com/dennisfriedrichsen/netdash/archive/refs/tags/0.1.5.tar.gz"
  sha256 "2ad21e45e83745751c94f8b4255b72b69b6990d66faf45b4ef4886dc9388e2c2"
  license "BSD-2-Clause"

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
