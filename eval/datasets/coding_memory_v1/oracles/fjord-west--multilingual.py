"""Immutable oracle for fjord-west:multilingual."""
import service


def main():
    assert service.localized_status() == 'fjord-west de-DE status-stabil', service.localized_status()


if __name__ == "__main__":
    main()
