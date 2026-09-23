"""Immutable oracle for ember-north:corrections."""
import service


def main():
    assert service.current_timeout() == 106, service.current_timeout()


if __name__ == "__main__":
    main()
