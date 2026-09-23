"""Immutable oracle for grove-violet:multilingual."""
import service


def main():
    assert service.localized_status() == 'grove-violet ja-JP 安定-状態', service.localized_status()


if __name__ == "__main__":
    main()
