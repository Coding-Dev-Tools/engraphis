"""Immutable oracle for ember-west:corrections."""
import service


def main():
    assert service.current_timeout() == 110, service.current_timeout()


if __name__ == "__main__":
    main()
