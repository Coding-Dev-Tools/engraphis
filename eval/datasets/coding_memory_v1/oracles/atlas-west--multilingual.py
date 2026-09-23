"""Immutable oracle for atlas-west:multilingual."""
import service


def main():
    assert service.localized_status() == 'atlas-west de-DE status-stabil', service.localized_status()


if __name__ == "__main__":
    main()
