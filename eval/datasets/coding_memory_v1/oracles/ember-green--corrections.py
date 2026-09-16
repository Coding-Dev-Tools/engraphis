"""Immutable oracle for ember-green:corrections."""
import service


def main():
    assert service.current_timeout() == 114, service.current_timeout()


if __name__ == "__main__":
    main()
