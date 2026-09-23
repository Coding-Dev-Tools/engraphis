"""Immutable oracle for juniper-west:corrections."""
import service


def main():
    assert service.current_timeout() == 170, service.current_timeout()


if __name__ == "__main__":
    main()
